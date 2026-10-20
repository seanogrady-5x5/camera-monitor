"""Polling, clip download and retention cleanup for Frigate review items
(alerts and detections). Called from the fetcher loop."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

try:
    from app.common import (
        CLIPS_DIR, RETAIN_DISCARD, RETAIN_NEW, camera_id_for, clip_path,
        frigate_base, get_servers,
    )
except ImportError:
    from common import (
        CLIPS_DIR, RETAIN_DISCARD, RETAIN_NEW, camera_id_for, clip_path,
        frigate_base, get_servers,
    )

log = logging.getLogger("fetcher.reviews")

CLIP_TIMEOUT = 60.0

UPSERT_REVIEW = """
INSERT INTO review_items
    (server, review_id, camera_name, camera_id, severity, start_time, end_time,
     objects, thumb_path, first_seen_utc, updated_utc)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (server, review_id) DO UPDATE SET
    camera_id   = excluded.camera_id,
    severity    = excluded.severity,
    end_time    = excluded.end_time,
    objects     = excluded.objects,
    thumb_path  = excluded.thumb_path,
    updated_utc = excluded.updated_utc
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def poll_reviews(conn: sqlite3.Connection, client: httpx.Client, limit: int) -> tuple[int, int]:
    """Fetch recent review items from each server and upsert them.

    Returns (items_seen, servers_failed).
    """
    seen = 0
    failed = 0
    for srv in get_servers(conn):
        server, port = srv["server"], srv["port"]
        try:
            resp = client.get(f"{frigate_base(server, port)}/api/review", params={"limit": limit})
            resp.raise_for_status()
            items = resp.json()
        except Exception as exc:
            failed += 1
            log.warning("review poll %s: %s", server, exc)
            continue

        now = utc_now()
        for it in items:
            camera_name = it.get("camera", "")
            camera_id = camera_id_for(conn, server, camera_name)
            data = it.get("data") or {}
            conn.execute(UPSERT_REVIEW, (
                server,
                it["id"],
                camera_name,
                camera_id,
                it.get("severity"),
                it.get("start_time"),
                it.get("end_time"),
                json.dumps(data.get("objects") or []),
                it.get("thumb_path"),
                now,
                now,
            ))
            seen += 1
        conn.commit()
    return seen, failed


def download_pending_clips(conn: sqlite3.Connection, client: httpx.Client, max_per_cycle: int) -> tuple[int, int]:
    """Download clips for completed, non-discarded review items that have a
    matched camera. Returns (downloaded, failed)."""
    rows = conn.execute(
        """SELECT * FROM review_items
           WHERE clip_status = 'pending' AND end_time IS NOT NULL
             AND retention != ? AND camera_id IS NOT NULL
           ORDER BY start_time DESC LIMIT ?""",
        (RETAIN_DISCARD, max_per_cycle),
    ).fetchall()

    # Review items whose camera is not in our DB can never be served; mark them
    # so we stop reconsidering them every cycle.
    conn.execute(
        "UPDATE review_items SET clip_status = 'unavailable', updated_utc = ? "
        "WHERE clip_status = 'pending' AND camera_id IS NULL",
        (utc_now(),),
    )
    conn.commit()

    downloaded = failed = 0
    servers = {s["server"]: s["port"] for s in get_servers(conn)}
    for row in rows:
        server = row["server"]
        port = servers.get(server)
        if port is None:
            continue
        start, end = row["start_time"], row["end_time"]
        url = f"{frigate_base(server, port)}/api/{row['camera_name']}/start/{start}/end/{end}/clip.mp4"
        path = clip_path(server, row["review_id"])
        tmp = path.with_suffix(".mp4.tmp")
        try:
            with client.stream("GET", url, timeout=CLIP_TIMEOUT) as resp:
                resp.raise_for_status()
                size = 0
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
                        size += len(chunk)
            if size == 0:
                raise RuntimeError("empty clip from Frigate")
            os.replace(tmp, path)
            conn.execute(
                "UPDATE review_items SET clip_status='downloaded', clip_path=?, clip_bytes=?, "
                "clip_error=NULL, updated_utc=? WHERE id=?",
                (path.name, size, utc_now(), row["id"]),
            )
            downloaded += 1
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            conn.execute(
                "UPDATE review_items SET clip_status='failed', clip_error=?, updated_utc=? WHERE id=?",
                (str(exc), utc_now(), row["id"]),
            )
            failed += 1
            log.warning("clip %s/%s: %s", server, row["review_id"], exc)
        conn.commit()
    return downloaded, failed


def _delete_clip(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    if row["clip_path"]:
        (CLIPS_DIR / row["clip_path"]).unlink(missing_ok=True)
    conn.execute(
        "UPDATE review_items SET clip_status='deleted', clip_path=NULL, clip_bytes=NULL, updated_utc=? WHERE id=?",
        (utc_now(), row["id"]),
    )


def cleanup(conn: sqlite3.Connection, new_retention_days: int) -> int:
    """Delete clips that should no longer be kept:
    - discarded items,
    - keep7 items past their expiry,
    - undecided ('new') items older than new_retention_days.
    Returns the number of clips removed."""
    now = utc_now()
    removed = 0

    # Discarded clips still on disk
    for row in conn.execute(
        "SELECT * FROM review_items WHERE retention=? AND clip_path IS NOT NULL", (RETAIN_DISCARD,)
    ).fetchall():
        _delete_clip(conn, row)
        removed += 1

    # keep7 past expiry
    for row in conn.execute(
        "SELECT * FROM review_items WHERE retain_until_utc IS NOT NULL "
        "AND retain_until_utc < ? AND clip_path IS NOT NULL", (now,)
    ).fetchall():
        _delete_clip(conn, row)
        removed += 1

    # Auto-expire undecided clips so storage stays bounded
    cutoff = (datetime.now(timezone.utc) - timedelta(days=new_retention_days)).isoformat(timespec="seconds")
    for row in conn.execute(
        "SELECT * FROM review_items WHERE retention=? AND first_seen_utc < ? AND clip_path IS NOT NULL",
        (RETAIN_NEW, cutoff),
    ).fetchall():
        _delete_clip(conn, row)
        removed += 1

    if removed:
        conn.commit()
    return removed


def run_once(conn: sqlite3.Connection, client: httpx.Client, cfg: dict) -> None:
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    limit = int(cfg.get("review_limit", "50"))
    max_clips = int(cfg.get("clip_max_per_cycle", "6"))
    new_days = int(cfg.get("new_retention_days", "3"))

    seen, srv_failed = poll_reviews(conn, client, limit)
    downloaded, dl_failed = download_pending_clips(conn, client, max_clips)
    removed = cleanup(conn, new_days)
    if seen or downloaded or dl_failed or removed:
        log.info(
            "reviews: %d seen (%d servers failed), %d clips downloaded, %d failed, %d removed",
            seen, srv_failed, downloaded, dl_failed, removed,
        )
