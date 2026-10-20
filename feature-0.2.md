# Feature 0.2 — Alerts & detections (review clips)

Branch: `feature-0.2`. Built against Frigate **0.17.2** using its `/api/review` endpoint
(review items carry a unique `id`, `severity` of `alert`/`detection`, `camera`, time range and objects).

---

1. list and log new alerts and detections per camera. log each alert and detection in the local sqlite db. if frigate uses a unique identifier for each event retain this as the unique identifier plus the server instance name as this solution supports multiple servers at once

**Outcome — done.**
- The fetcher polls `GET /api/review?limit=<review_limit>` on each distinct Frigate server (derived from the enabled cameras' `frigate_host`) and upserts every item into a new `review_items` table.
- Each item is uniquely keyed by **`UNIQUE (server, review_id)`** — `server` is the Frigate instance (`frigate_host`), `review_id` is Frigate's own review id (e.g. `1792471579.98854-5xl83i`). Several servers can therefore coexist without id collisions, as asked.
- Stored per item: `camera_name`, matched `camera_id` (our cameras row, nullable), `severity`, `start_time`/`end_time`, `objects` (JSON), `thumb_path`, timestamps, and clip/retention state.
- Polling runs on its own cadence (`review_interval`, default 30s) inside the fetcher loop, independent of the image fetch interval. Re-polling updates `end_time` and metadata for in-progress items.
- Verified against the real Frigate: a poll logged 9 items across cameras (fox/raccoon/car/squirrel detections and alerts); `fetcher.reviews: 9 seen (0 servers failed)`.
- Config keys (in `backend/app/config.csv`): `review_enabled`, `review_interval`, `review_limit`.

2. download the video file from each alert / detection and store it in a local location on the backend container

**Outcome — done.**
- For each completed item (has `end_time`) with a matched camera, the fetcher downloads the clip from Frigate's recording export endpoint `GET /api/<camera>/start/<start>/end/<end>/clip.mp4` (the reliable per-review clip in 0.17; `/api/review/<id>/preview.*` is 404 on this version).
- Clips are streamed to disk at **`/data/clips/<server>__<review_id>.mp4`** (on the shared `camera-data` volume, so both `fetcher` and `api` see them), written to a `.tmp` file and atomically renamed. Size and status are recorded (`clip_status`: `pending|downloaded|failed|unavailable|deleted`).
- Downloads are throttled to `clip_max_per_cycle` (default 6) per cycle with a 60s per-clip timeout, so a backlog doesn't stall the loop. Items whose camera isn't in our DB are marked `unavailable` (never served) instead of being retried forever.
- **Retention cleanup** each cycle deletes clips that should no longer exist: discarded items, `keep7` items past expiry, and undecided (`new`) items older than `new_retention_days` (default 3) so storage stays bounded.
- Verified against real Frigate: 8 clips downloaded to `/data/clips` (≈17 MB total), sizes recorded (e.g. 12 MB, 3.5 MB, plus sub-second 804-byte clips).
- Config keys: `clip_max_per_cycle`, `new_retention_days`.
- **Per-camera clip toggle:** a `download_clips` column on `cameras` (checkbox in the admin Cameras table, default on) lets you turn clip downloads off for busy/uninteresting cameras. Such cameras still have their alerts *logged* (marked `skipped`, not `pending`), so nothing is fetched over Starlink and they don't appear in review playback. Applies to new alerts on the next cycle.
- **Note / caveat:** clips are pulled from the remote Frigate over Starlink, so this uses real bandwidth. It fetches *both* alerts and detections as specified; if that proves heavy you can raise `review_interval`, lower `clip_max_per_cycle`, or (future) restrict to `alert` severity.

3. allow the user to switch to a mode which plays the alerts / detections one after the other, auto-playing the videos full screen on the viewer frontend with the top-bar still in place. have three buttons which allow the user to discard the clip, retain it for 7 days or retain it forever

**Outcome — done.**
- New **review modes** in the monitor: `New` auto-plays undecided clips one after another, full-screen (`<video>` in the main area, `object-fit: contain`), with the **top bar still in place** (camera name, `SEVERITY · objects · duration · time`, position `n of N`, and the live stats). Videos are muted + `playsinline` so browsers allow autoplay; on `ended` the player advances to the next clip, looping back to a refreshed list at the end.
- Three buttons overlay the bottom of the video, exactly as asked: **Discard** (red), **Keep 7 days** (green), **Keep forever** (green). Each `POST /api/reviews/{id}/retention`, removes the item from the queue and advances. `keep7` sets `retain_until_utc = now + 7 days`; cleanup deletes the clip when it expires. `discard` deletes the clip on the next cleanup.
- A `Retained` mode plays kept clips with **Discard**, **Keep forever** and **Skip**.
- Backend endpoints (all frontend-scoped, gated by a new `allow_review` frontend flag):
  `GET /api/reviews?status=new|retained`, `GET /api/reviews/{id}/clip.mp4` (serves Range requests for seeking), `POST /api/reviews/{id}/retention`.
- Scoping verified: a viewer only ever sees review items for cameras mapped to its frontend. `Range` streaming returns `206`. Keep-7 then Discard → cleanup removes the file → clip `404`.
- Verified in a headless browser: switched to `New`, a clip loaded (`readyState 4`) and played, the three buttons showed, **Keep 7 days** advanced the queue (7 → 6) and the item appeared under `Retained`; leaving the mode paused playback.

4. make the mode selector part of the top bar somehow A) current camera grid B) current camera single C) new alerts and detections D) retained alerts and detections

**Outcome — done.**
- A segmented **mode selector** sits at the right of the top bar: **Camera** (single, B), **Grid** (A), **New** (C), **Retained** (D). The active mode is highlighted; **New** carries a red badge with the count of undecided clips (polled each tick).
- Switching modes starts/stops the right behaviour (camera rotation pauses in review modes; the video player stops when leaving them). Tapping the single image still jumps to Grid; tapping a grid tile returns to that camera.
- `New`/`Retained` only appear when the frontend's `allow_review` is on. A new **Review** checkbox in the admin Frontends table toggles it per frontend. Verified: with `allow_review` off, the public frontend shows only **Camera | Grid**, and its `/api/reviews` returns `403`.

---

## Summary of changes

- `backend/app/common.py` — `review_items` table (+ index), `CLIPS_DIR`, retention constants, `allow_review` column on `frontends` with a lightweight column migration for existing databases, and review/server/clip helpers.
- `backend/app/reviews.py` *(new)* — poll, clip download, and retention cleanup; called once per `review_interval` from the fetcher loop.
- `backend/app/fetcher.py` — runs the review cycle on its own cadence.
- `backend/app/main.py` — `/api/reviews`, `/api/reviews/{id}/clip.mp4`, `/api/reviews/{id}/retention`; `allow_review` in the frontend model, admin list and `/api/config`.
- `backend/app/static/admin.html` — **Review** toggle column in the Frontends table.
- `backend/app/config.csv` — `review_enabled`, `review_interval`, `review_limit`, `clip_max_per_cycle`, `new_retention_days`.
- `frontend/public/index.html` — 4-mode model, top-bar mode selector with New badge, full-screen review player and retention buttons.

## Operational notes

- Clips live on the `camera-data` volume at `/data/clips`; they survive restarts and are shared by `fetcher` (writer) and `api` (reader).
- Storage is bounded by retention: undecided clips auto-expire after `new_retention_days`, `keep7` after 7 days; only `keep_forever` persists indefinitely.
- Multi-server support is built in via the `(server, review_id)` key and per-server polling, though the current deployment has a single Frigate (`neptune.bonaparte.lcars`).
- Not yet done / possible follow-ups: severity filter to download alerts only (bandwidth), thumbnails in a review list/overview, and marking items reviewed back in Frigate (`/api/reviews/viewed`).
