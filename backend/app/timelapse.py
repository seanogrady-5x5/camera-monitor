"""Hourly timelapse compiler. For each timelapse-enabled camera it builds a
single rolling video from the last 24h of retained captures with ffmpeg (CPU
encoding for now; GPU later), replaces the previous auto timelapse unless the
user kept it, purges captures older than the window, and expires kept clips."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from app.common import (
        CAPTURES_DIR, TIMELAPSE_DIR, TL_AUTO, _safe, capture_dir, connect,
        delete_capture_rows, init_db, load_config, timelapse_cameras, timelapse_dir,
    )
except ImportError:
    from common import (
        CAPTURES_DIR, TIMELAPSE_DIR, TL_AUTO, _safe, capture_dir, connect,
        delete_capture_rows, init_db, load_config, timelapse_cameras, timelapse_dir,
    )

log = logging.getLogger("timelapse")

TS_RE = re.compile(r"_(\d{8})_(\d{6})\.jpg$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _capture_time(path: Path) -> datetime:
    """Capture time from the filename (UTC), falling back to file mtime."""
    m = TS_RE.search(path.name)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _window_frames(camera: dict, cutoff: datetime) -> list[tuple[datetime, Path]]:
    cdir = capture_dir(camera)
    if not cdir.is_dir():
        return []
    frames = []
    for p in cdir.glob("*.jpg"):
        t = _capture_time(p)
        if t >= cutoff:
            frames.append((t, p))
    frames.sort(key=lambda x: x[0])
    return frames


def build_timelapse(camera: dict, frames: list[tuple[datetime, Path]], fps: int, height: int) -> dict | None:
    """Encode frames into an mp4. Returns metadata, or None on failure."""
    out_dir = timelapse_dir(camera["id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    # Same naming as image captures: <instance>_<camera>_<yyyymmdd>_<hhmmss>.mp4
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"{_safe(camera['frigate_host'])}_{_safe(camera['frigate_camera_name'])}_{stamp}.mp4"

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as lst:
        for _, p in frames:
            lst.write(f"file '{p.as_posix()}'\n")
        list_path = lst.name

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-r", str(fps), "-i", list_path,
        # Fixed height, width auto-rounded to an even number (libx264 needs even
        # dimensions); unifies captures of differing source resolutions.
        "-vf", f"scale=-2:{height},setsar=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
        out.as_posix(),
    ]
    started = time.monotonic()
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
    except subprocess.CalledProcessError as exc:
        log.warning("ffmpeg %s: %s", camera["frigate_camera_name"], exc.stderr.decode(errors="replace")[:200])
        out.unlink(missing_ok=True)
        return None
    except Exception as exc:
        log.warning("ffmpeg %s: %s", camera["frigate_camera_name"], exc)
        out.unlink(missing_ok=True)
        return None
    finally:
        os.unlink(list_path)

    return {
        "path": f"{camera['id']}/{out.name}",
        "bytes": out.stat().st_size,
        "frame_count": len(frames),
        "build_seconds": round(time.monotonic() - started, 2),
        "covers_start_utc": frames[0][0].isoformat(timespec="seconds"),
        "covers_end_utc": frames[-1][0].isoformat(timespec="seconds"),
    }


def _delete_timelapse_file(row) -> None:
    if row["path"]:
        (TIMELAPSE_DIR / row["path"]).unlink(missing_ok=True)


def process_camera(conn, camera: dict, cutoff: datetime, fps: int, height: int, min_frames: int) -> bool:
    frames = _window_frames(camera, cutoff)
    if len(frames) < min_frames:
        log.info("camera %s: only %d frames, skipping", camera["camera_rename"], len(frames))
        return False

    meta = build_timelapse(camera, frames, fps, height)
    if meta is None:
        return False

    now = utc_now()
    with conn:
        # Remove the previous auto timelapse for this camera (kept ones survive)
        for row in conn.execute(
            "SELECT * FROM timelapses WHERE camera_id = ? AND retention = ?", (camera["id"], TL_AUTO)
        ).fetchall():
            _delete_timelapse_file(row)
            conn.execute("DELETE FROM timelapses WHERE id = ?", (row["id"],))
        conn.execute(
            """INSERT INTO timelapses
               (server, camera_id, camera_name, created_utc, covers_start_utc, covers_end_utc,
                frame_count, path, bytes, build_seconds, retention)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (camera["frigate_host"], camera["id"], camera["frigate_camera_name"], now,
             meta["covers_start_utc"], meta["covers_end_utc"], meta["frame_count"],
             meta["path"], meta["bytes"], meta["build_seconds"], TL_AUTO),
        )
    log.info("camera %s: %d frames -> %s (%.1f MB, %.1fs)",
             camera["camera_rename"], meta["frame_count"], meta["path"],
             meta["bytes"] / 1e6, meta["build_seconds"])
    return True


def purge_captures(conn, cutoff: datetime) -> int:
    """Delete capture rows (and their files) older than the window, keeping the
    DB and disk in sync, then sweep any orphan files left on disk."""
    cutoff_s = cutoff.isoformat(timespec="seconds")
    removed = 0
    with conn:
        rows = conn.execute("SELECT * FROM captures WHERE created_utc < ?", (cutoff_s,)).fetchall()
        removed = delete_capture_rows(conn, rows)
        # Orphan files (e.g. pre-dating this table, or rows deleted elsewhere)
        tracked = {r["path"] for r in conn.execute("SELECT path FROM captures")}
        if CAPTURES_DIR.is_dir():
            for p in CAPTURES_DIR.glob("*/*.jpg"):
                rel = f"{p.parent.name}/{p.name}"
                if rel not in tracked and _capture_time(p) < cutoff:
                    p.unlink(missing_ok=True)
    return removed


def expire_timelapses(conn) -> int:
    """Delete kept7 timelapses past their expiry."""
    now = utc_now()
    removed = 0
    with conn:
        for row in conn.execute(
            "SELECT * FROM timelapses WHERE retain_until_utc IS NOT NULL AND retain_until_utc < ? AND path IS NOT NULL",
            (now,),
        ).fetchall():
            _delete_timelapse_file(row)
            conn.execute("DELETE FROM timelapses WHERE id = ?", (row["id"],))
            removed += 1
    return removed


def run_once(conn, cfg: dict) -> None:
    window_h = int(cfg.get("timelapse_window_hours", "24"))
    fps = int(cfg.get("timelapse_fps", "30"))
    height = int(cfg.get("timelapse_height", "480"))
    min_frames = int(cfg.get("timelapse_min_frames", "2"))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_h)

    cameras = timelapse_cameras(conn)
    built = sum(process_camera(conn, c, cutoff, fps, height, min_frames) for c in cameras)
    purged = purge_captures(conn, cutoff)
    expired = expire_timelapses(conn)
    log.info("timelapse run: %d/%d cameras built, %d captures purged, %d expired",
             built, len(cameras), purged, expired)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    if cfg.get("timelapse_enabled", "true").lower() in ("false", "0", "no"):
        log.info("timelapse disabled; idling")
        while True:
            time.sleep(3600)

    interval = int(cfg.get("timelapse_interval", "3600"))
    init_db()
    TIMELAPSE_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    log.info("timelapse runner: every %ds", interval)
    while True:
        started = time.monotonic()
        try:
            run_once(conn, load_config())
        except Exception as exc:
            log.warning("timelapse cycle error: %s", exc)
        time.sleep(max(0.0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
