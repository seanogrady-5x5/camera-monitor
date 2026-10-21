# Feature 0.3 — Retained captures & timelapses

Branch: `feature-0.3` (off the committed 0.2 work). Timelapse encoding uses **ffmpeg on CPU** for now (GPU later — `libx264`, `-preset veryfast`).

---

1) change the image capture routine to place (and retain) each capture as a file with the name <instance>_<camera><yyyymmdd>_<hhmmss> so that they can be later processed

**Outcome — done.**
- The fetcher now writes every capture to a retained, timestamped file instead of overwriting one: `CAPTURES_DIR/<camera_id>/<instance>_<camera>_<yyyymmdd>_<hhmmss>.jpg` (UTC, e.g. `neptune.bonaparte.lcars_Driveway_Zoom_20261020_202104.jpg`). Written to a `.tmp` then atomically renamed.
- Files live per-camera under `/data/captures/<camera_id>/` (on the shared `camera-data` volume), so purge and timelapse globbing are simple.
- Cameras with **Timelapse off** keep only the newest capture (the previous file is deleted after the DB pointer moves), so busy/uninteresting cameras don't accumulate history.

2) change the frontend to show images based on the camera database records. when a new capture is captured, the daemon will update the database record to point to the newest file

**Outcome — done.**
- `fetches` gained a `latest_image` column (relative path). On each successful capture the fetcher updates it to the new file.
- `GET /api/frame/{id}` now serves the file the DB points to, rather than a fixed overwrite path. The frontend is unchanged in behaviour — it still shows the newest frame — but it's now backed by the retained, DB-tracked captures.

3) a new separate runner will compile the images into a timelapse video using ffmpeg for the previous 24h. Any images older than 24h will be purged. each hour this process will run for the last 24h and eliminate the previous timelapse unless the user elects to keep it

**Outcome — done.**
- New **`timelapse` service** (same backend image, `python timelapse.py`), runs every `timelapse_interval` (default 3600s = hourly).
- Per **enabled, timelapse-on** camera it gathers captures from the last `timelapse_window_hours` (24) and encodes a single mp4 with ffmpeg (concat demuxer → `libx264`, scaled to a uniform even-width `timelapse_height`, default 480). One video per camera.
- Captures older than 24h are purged each run; the previous **auto** timelapse for a camera is deleted and replaced, **unless** the user kept it (keep7/keep_forever survive). keep7 timelapses are deleted when their 7 days expire.
- A `timelapses` table tracks each video (server, camera, created/covers times, frame count, path, bytes, retention, expiry).
- Verified end to end against the real cameras: 12 per-camera videos built (854×480 h264), replace-unless-kept confirmed (kept=1 survived, auto=12 replaced), captures purged, expiry works.
- ffmpeg added to the backend image (`apt-get install ffmpeg`). Config keys: `timelapse_enabled`, `timelapse_interval`, `timelapse_fps` (30), `timelapse_window_hours` (24), `timelapse_height` (480), `timelapse_min_frames`.

4) add a new timelapse mode in the upper right which will rotate through the timelapses. if a user clicks keep 7 or keep forever it follows the same mechanism as alerts

**Outcome — done.**
- A 5th **Timelapses** mode (film-strip icon) joins Camera · Grid · New · Retained in the top-right selector. It auto-plays each camera's timelapse one after another, full-screen with the top bar intact (camera, `TIMELAPSE · N frames · to <time> · KEPT…`, position, stats, progress bar).
- Buttons **Keep 7 days** / **Keep forever** (plus **Skip**) use the same retention mechanism as alerts, via `POST /api/timelapses/{id}/retention`. Keeping marks the video so the hourly runner won't replace it.
- Gated by a per-frontend **Timelapse** flag (admin Frontends table). Verified: with it off, the public frontend hides the mode and `/api/timelapses` returns `403`.
- API: `GET /api/timelapses`, `GET /api/timelapses/{id}/video.mp4` (Range-enabled), `POST /api/timelapses/{id}/retention` — all frontend-scoped to mapped cameras.
- Verified in a headless browser: 5 modes present, timelapse video plays, correct metadata and buttons, Keep forever advances and persists.

---

## Also fixed (pre-existing bug, now important)

- **Frigate resize param was wrong.** `frigate_url` used `h=<height>`, which Frigate 0.17 ignores — captures were always full 1080p (~385 KB). The correct param is `height=`. Fixed, so a camera's `height` setting now actually downscales (captures dropped to 853×480, ~77 KB). This matters a lot now that captures are retained: ~**2 GB/day** at 480p vs ~8 GB at 1080p for ~15 cameras at a 60s interval (purged daily). Lower `fetch_interval`/`height`/`quality` to tune.
- Fixed an inverted ok/failed counter in the fetcher log (tuple index shifted when captures were added).

## Admin additions

- Cameras table: **T-lapse** checkbox (`make_timelapse`, default on) beside **Clips** — turn off to skip retained captures + timelapse for a camera.
- Frontends table: **Timelapse** checkbox (`allow_timelapse`, default on).

## Per-camera storage note

Retained captures are the main new storage cost. At 480p/60s that's ~1440 files/camera/day, purged at 24h. On the high-capacity off-site host this is fine; the per-camera T-lapse toggle excludes cameras you don't care about.

## Possible follow-ups

- GPU encoding (NVENC/VAAPI) in place of `libx264` once hardware is available.
- A capture interval independent of the display fetch interval (denser or sparser timelapses).
- Thumbnails / a scrubber for the timelapse list.
