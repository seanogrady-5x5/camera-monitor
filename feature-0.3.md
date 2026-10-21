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
- ffmpeg added to the backend image (`apt-get install ffmpeg`). Settings: `timelapse_enabled`, `timelapse_interval`, `timelapse_fps` (30), `timelapse_window_hours` (24), `timelapse_height` (480), `timelapse_min_frames` (now in the admin Settings section).

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

## Added later — per-camera metrics on the admin panel

1) **captures on disk per camera** — a new `captures` table holds one row per retained file (camera, path, bytes, created). The admin Cameras **Status** cell shows the count (`N imgs`). Deletions keep DB and disk in lockstep: the timelapse purge and the "keep only newest" cleanup both delete the file and its row together (`delete_capture_rows`), so the count is always accurate from the database.
2) **total image size per camera** — each `captures` row stores the file's `bytes`; the admin sums them per camera and shows `X MB` (switching to `GB` ≥ 1000 MB).
3) **last timelapse processing time per camera** — `timelapses` gained a `build_seconds` column (the ffmpeg encode time); the admin shows the most recent timelapse's build time (`t-lapse Xs`, or `no t-lapse`).

All three are read from the database (not the filesystem) and surfaced via `/api/admin/cameras` (`capture_count`, `capture_bytes`, `last_timelapse_seconds`, `last_timelapse_utc`). Verified: purge deletes row+file together, off-camera prune keeps exactly one, and the metrics render per camera.

A **totals line** under the Cameras table sums these across all cameras: "Total: N cameras (M enabled) · X images · Y MB/GB on disk" (refreshed on every load/save/revert).

Timelapse storage was added alongside: each camera's status line shows its timelapse count and size (summed from `timelapses.bytes`, auto + kept), and the totals line adds "K timelapses B MB" plus a combined images+timelapses "on disk" figure. API fields: `timelapse_count`, `timelapse_bytes`.

## Added later — timelapse download

- **Download while playing**: in Timelapses mode the top-bar download icon (same control as the still-image download in Camera mode) downloads the currently-playing video via `GET /api/timelapses/{id}/video.mp4?download=1`, which returns it as an attachment. Inline playback (no `download`) is unchanged, so the `<video>` still streams/seeks. Gated by the frontend's `allow_download` (public → `403`).
- Timelapses are **already recorded in the DB** (`timelapses` table) and a **kept** timelapse's row and file remain — only the rolling `auto` one is replaced hourly; `keep7`/`keep_forever` survive (and `keep7` is removed only when its 7 days expire).
- Timelapse **filenames now match image captures exactly**: `<instance>_<camera>_<yyyymmdd>_<hhmmss>.mp4`, using the same `_safe` sanitization (e.g. `neptune.bonaparte.lcars_Driveway_Zoom_20261021_042335.mp4`). This is also the download filename.

## Added later — seamless (double-buffered) clip playback

On Android, advancing between clips briefly showed the native full-screen play-arrow overlay (~0.5s), because the single `<video>` reset its `src` → `load()` → `play()` synchronously and sat empty/paused while decoding the next clip's first frame. Fixed by **double-buffering**: two stacked `<video>` elements, with the next clip preloaded in the idle one and simply revealed+played on advance (`frontend/public/index.html` only). The visible video is never emptied, so there's no paused state for Android to draw the overlay on. Manual Skip/retention jumps use the non-preloaded path (not the reported case). Also dropped the per-src `?_=timestamp` cache-buster (clip/timelapse ids are immutable, so caching + preload now help). Verified the active buffer alternates on each advance with the next clip preloaded (`readyState 4`); the Android visual is the user's final confirmation.

## Added later — settings moved from config.csv into the database / admin

- `backend/app/config.csv` is **removed**. All 14 global settings now live in a `settings` table (overrides only) and are edited in a new **Settings** section of the admin page, grouped General / Capture / Reviews / Timelapse, staged into the same Save/Revert.
- `CONFIG_SPEC` in `common.py` defines every key's type, default, range, group, label and help. Defaults equal the previous `config.csv` values, so behaviour is unchanged until something is edited. An unset key always falls back to its default.
- API: `GET /api/admin/settings`, `PUT /api/admin/settings` (validated: unknown key / wrong type / out of range → `422`).
- The fetcher and timelapse runner re-read settings every cycle and `/api/config` reads `rotation_interval` per request, so edits apply without a rebuild or restart — except `fetch_workers` (thread pool, fetcher restart).
- Verified: defaults load on an empty table, a live `rotation_interval` change appeared in `/api/config` immediately, invalid values were rejected in the API and the admin toast, Revert restored staged edits.

## Possible follow-ups

- GPU encoding (NVENC/VAAPI) in place of `libx264` once hardware is available.
- A capture interval independent of the display fetch interval (denser or sparser timelapses).
- Thumbnails / a scrubber for the timelapse list.
