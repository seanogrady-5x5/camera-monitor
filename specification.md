# Camera Monitor — Specification

## Purpose

A lightweight web app that rotates through a list of cameras, displaying a single still frame per camera per rotation. The goal is to monitor remote cameras over a low-bandwidth Starlink connection (~20–30 Mbps upload) without the overhead of live streaming relatively static scenes.

Frames are sourced from an existing [Frigate](https://frigate.video/) NVR instance via its HTTP API (`/api/<camera>/latest.jpg?height=<h>&quality=<q>`), rather than by connecting to cameras directly. Frigate downscales and recompresses the snapshot server-side, keeping the per-frame payload small.

Site stats (temperatures, battery, inverter) shown in the top bar are read from [Home Assistant](https://www.home-assistant.io/) via its REST API.

Beyond the live rotation, the app also:

- **logs alerts and detections** from Frigate's `/api/review` feed, downloads each event's clip, and lets a viewer play them back and decide to discard / keep 7 days / keep forever;
- **retains every capture** as a timestamped file and compiles a rolling **24-hour timelapse** per camera (hourly, with ffmpeg), which the viewer can also play back and keep.

## Architecture

Two independent apps, each with its own directory, Dockerfile and image:

| App | Directory | Image | Contents |
|---|---|---|---|
| **Backend** | `backend/` | `camera-monitor-backend` | Python: the fetcher, the JSON/image API, and the admin page (`/admin`). |
| **Frontend** | `frontend/` | `camera-monitor-frontend` | nginx serving the read-only monitor page and proxying `/api/*` (except `/api/admin/*`) to the backend. No Python. |

They run as containers (one `frontend` per audience):

| Service | Image | Command | Role |
|---|---|---|---|
| `fetcher` | backend | `python fetcher.py` | Polls Frigate (captures) and Home Assistant (stats) on a fixed schedule, and Frigate's `/api/review` (alerts/detections) on its own cadence; retains each capture as a timestamped file, downloads event clips, and writes all records to SQLite. The only service with the HA token |
| `timelapse` | backend | `python timelapse.py` | Hourly: compiles each camera's last 24h of captures into an mp4 with **ffmpeg** (CPU/`libx264` for now, GPU later), purges captures past the window, and expires timelapses |
| `api` | backend | `uvicorn main:app --port 8011` | **Unauthenticated** JSON/image/video API and admin page (intended for a private network). Reads captures, clips, timelapses and records; reads/writes all configuration. Only contacts Frigate for the original-image download |
| `frontend` | frontend | nginx on port **8010** (8020, …) | Serves `index.html` (monitor); forwards `/api/*` to `API_UPSTREAM` with an `X-Frontend-Name` header set from `FRONTEND_NAME`, so the browser only ever talks to the frontend origin (no CORS needed). Refuses `/api/admin/*` with `403`, so configuration can only be changed on the backend port |

`fetcher`, `timelapse` and `api` share the `camera-data` volume mounted at `/data`. The frontends have no access to it.

```
Frigate ─────────HTTP──▶ fetcher ──▶ /data/captures/<cam>/*.jpg   (retained stills)
Home Assistant ──HTTP──▶    │    ├─▶ /data/clips/*.mp4            (alert/detection clips)
                            │    └─▶ /data/camera-monitor.db
                 timelapse ─┴─────▶ /data/timelapses/<cam>/*.mp4  (hourly 24h videos)
                                          ▲
                                    api :8011
                                          ▲  /api/*
                    browser ──▶ frontend (nginx) :8010 / :8020    (monitor, per audience)
             admin browser ──▶ api :8011/admin                     (configuration)
```

### Multiple audiences (frontends)

The backend holds a mapping of cameras to named **frontends**. Each frontend container is started with `FRONTEND_NAME=<name>`; its nginx sends that name to the backend as `X-Frontend-Name` on every `/api/*` request, **overwriting any value the browser sent**, so viewers cannot pick a different frontend. The backend only returns cameras mapped to that frontend, and applies the frontend's options (stats, download, error detail). Run one frontend container per audience (e.g. `main` on 8010 for everything, `public` on 8020 for a subset), each with its own port/hostname and its own authentication in front of it.

The backend trusts `X-Frontend-Name`, so **port 8011 must only be reachable from the private network**; anything that can reach it directly can request any frontend's view (and the admin API).

All configuration (cameras, HA entities, frontends and their camera mappings) lives in the same SQLite database, edited through the backend's admin page (`http://<host>:8011/admin`). Shared backend code (DB schema, migrations, seeding, config queries, Frigate URL building, capture/clip/timelapse paths) lives in `backend/app/common.py`.

### Repository layout

```
backend/
  Dockerfile              python:3.12-slim + ffmpeg; default CMD runs the API on 8011
  requirements.txt
  app/
    main.py               API (FastAPI)
    fetcher.py            capture + stats + review polling loop
    reviews.py            review (alert/detection) polling, clip download, cleanup
    timelapse.py          hourly timelapse runner (ffmpeg)
    common.py             DB schema, migrations, seeding, shared helpers
    static/admin.html     admin page (served by the API at /admin)
    cameras-sample.csv    example seed file
    cameras.csv           seed for the cameras table (gitignored, volume-mounted)
    ha-entities.csv       seed for the ha_entities table (volume-mounted)
frontend/
  Dockerfile              nginx:1.27-alpine
  nginx/default.conf.template
  public/
    index.html            monitor
docker-compose.yml        fetcher, timelapse, api, frontend(s) (Linux and Docker Desktop)
.env / .env.example       HA_URL / HA_TOKEN for the fetcher

# Data volume (camera-data, /data):
#   captures/<camera_id>/*.jpg     retained timestamped stills
#   clips/*.mp4                    downloaded alert/detection clips
#   timelapses/<camera_id>/*.mp4   compiled timelapse videos
#   camera-monitor.db              SQLite (WAL)
```

## Fetcher

- At startup, creates the DB tables and seeds configuration if empty (see Database).
- At the start of every cycle, re-reads enabled cameras and HA entities from the DB, so admin-page edits take effect on the next cycle without a restart.
- Every `fetch_interval` seconds (default **15**), fetches all enabled cameras in parallel using a thread pool of `fetch_workers` threads (default **8**).
- Each fetch is a single attempt with a 10-second timeout. There are no in-cycle retries; the next cycle retries.
- **Captures are retained.** On success it writes the JPEG to `/data/captures/<camera_id>/<frigate_host>_<frigate_camera_name>_<yyyymmdd>_<hhmmss>.jpg` (UTC; non-`[A-Za-z0-9._-]` characters become `_`), written to a `.tmp` sibling and atomically renamed, and updates `fetches.latest_image` to point at it. A camera with **`make_timelapse` off** keeps only the newest capture (the previous file is deleted once the pointer moves); a camera with it on retains every capture for the timelapse (purged at 24h by the timelapse runner).
- After each cycle, results are written to SQLite in one transaction.
- The next cycle starts `fetch_interval` seconds after the previous one *started*. If a cycle overruns the interval, the next one starts immediately.
- **Home Assistant**: in the same cycle and thread pool, fetches `GET <url>/api/states/<entity_id>` (header `Authorization: Bearer <token>`, 10 s timeout) for each distinct non-empty `entity_id` in the `ha_entities` table, storing `state`, `attributes.unit_of_measurement` and `last_updated`. If `HA_URL` or `HA_TOKEN` is unset or empty, HA polling is skipped (logged once at startup); camera fetching is unaffected.
- **Alerts & detections** (`reviews.py`): every `review_interval` seconds (default **30**, independent of the capture interval), for each distinct Frigate server it fetches `GET /api/review?limit=<review_limit>` and upserts each item into `review_items`. For completed items whose camera has `download_clips` on, it downloads the clip from `GET /api/<camera>/start/<start>/end/<end>/clip.mp4` to `/data/clips/<server>__<review_id>.mp4` (up to `clip_max_per_cycle` per cycle, 60 s timeout). Items for cameras with `download_clips` off are logged but marked `skipped`; items for unknown cameras are `unavailable`. A cleanup pass deletes clips for discarded items, expired `keep7` items, and undecided items older than `new_retention_days`.
- Failures are logged as warnings; each cycle logs a summary (`cameras N ok, M failed; HA N ok, M failed`).

## Timelapse runner

The `timelapse` service (`python timelapse.py`, same backend image which includes **ffmpeg**) runs every `timelapse_interval` seconds (default **3600**, hourly):

- For each enabled camera with **`make_timelapse` on**, it gathers captures from the last `timelapse_window_hours` (24), sorted by their filename timestamp, and encodes a single mp4 with ffmpeg: concat demuxer at `timelapse_fps` (30) → `libx264`, scaled to a uniform even-width `timelapse_height` (480) so captures of mixed source resolution still encode. One video per camera, needing at least `timelapse_min_frames` frames.
- The new video is written as `<instance>_<camera>_<yyyymmdd>_<hhmmss>.mp4` (same naming/sanitization as image captures) and inserted into `timelapses` with retention `auto`. The previous **auto** timelapse for that camera is deleted and replaced; timelapses the user kept (`keep7`/`keep_forever`) keep their row and file and survive. `keep7` timelapses are deleted when their 7 days expire.
- Captures older than the window are purged across all cameras.
- Encoding is CPU-bound today; GPU (NVENC/VAAPI) is a planned swap for the `libx264` step.

## Database

SQLite at `/data/camera-monitor.db` (WAL mode).

### Configuration tables

```sql
CREATE TABLE settings (                           -- global setting overrides only;
    key   TEXT PRIMARY KEY,                       -- missing keys use CONFIG_SPEC defaults
    value TEXT NOT NULL
);
```

Edited through the admin page. Ordered by `sort_order` ascending (NULLs last, then by `id`).

```sql
CREATE TABLE cameras (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sort_order          REAL,
    frigate_host        TEXT NOT NULL,
    frigate_port        INTEGER NOT NULL DEFAULT 5000,
    frigate_camera_name TEXT NOT NULL,
    camera_rename       TEXT NOT NULL,           -- display name
    enabled             INTEGER NOT NULL DEFAULT 1,
    quality             INTEGER,                 -- Frigate quality= (1–100)
    height              INTEGER,                 -- Frigate height= (downscale)
    download_clips      INTEGER NOT NULL DEFAULT 1,  -- download alert/detection clips?
    make_timelapse      INTEGER NOT NULL DEFAULT 1,  -- retain captures + build a timelapse?
    UNIQUE (frigate_host, frigate_camera_name)
);

CREATE TABLE ha_entities (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sort_order          REAL,
    label               TEXT NOT NULL,
    entity_id           TEXT NOT NULL DEFAULT '',  -- blank = placeholder slot
    decimals            INTEGER,
    unit                TEXT NOT NULL DEFAULT '',
    scale               REAL
);
```

```sql
CREATE TABLE frontends (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL UNIQUE,      -- matches a container's FRONTEND_NAME
    show_stats          INTEGER NOT NULL DEFAULT 1,
    allow_download      INTEGER NOT NULL DEFAULT 1,
    show_errors         INTEGER NOT NULL DEFAULT 1,
    allow_review        INTEGER NOT NULL DEFAULT 1,  -- expose New/Retained modes + review API
    allow_timelapse     INTEGER NOT NULL DEFAULT 1   -- expose Timelapses mode + timelapse API
);

CREATE TABLE frontend_cameras (                    -- which cameras each frontend shows
    frontend_id         INTEGER NOT NULL REFERENCES frontends(id) ON DELETE CASCADE,
    camera_id           INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    PRIMARY KEY (frontend_id, camera_id)
);
```

Connections enable `PRAGMA foreign_keys`, so deleting a camera or frontend removes its mappings.

**Seeding**: at startup every service runs `init_db()`, which creates any missing tables, applies additive column **migrations** to existing databases (e.g. `cameras.download_clips`, `cameras.make_timelapse`, `frontends.allow_review`, `frontends.allow_timelapse`, `fetches.latest_image`), and, under a `BEGIN IMMEDIATE` write lock (so it happens once even when several start together), imports `cameras.csv` into `cameras` if that table is empty and `ha-entities.csv` into `ha_entities` if that table is empty. CSV row order becomes `sort_order` (1, 2, 3…), except that `ha-entities.csv`'s own `order` column is used when present. If `frontends` is empty, a frontend named `main` is created and mapped to every camera. Once a table has rows, its CSV is ignored.

### Fetch tables

One row per camera / entity, upserted on each fetch:

```sql
CREATE TABLE fetches (
    frigate_host        TEXT NOT NULL,
    frigate_camera_name TEXT NOT NULL,
    last_attempt_utc    TEXT NOT NULL,   -- every attempt
    last_fetched_utc    TEXT,            -- last successful fetch
    payload_bytes       INTEGER,         -- size of last successful payload
    last_error          TEXT,            -- NULL after a success
    latest_image        TEXT,            -- newest capture, relative to captures/
    PRIMARY KEY (frigate_host, frigate_camera_name)
);

CREATE TABLE ha_states (
    entity_id           TEXT PRIMARY KEY,
    last_attempt_utc    TEXT NOT NULL,
    last_fetched_utc    TEXT,            -- last successful fetch
    state               TEXT,            -- raw HA state string
    unit                TEXT,            -- attributes.unit_of_measurement
    ha_last_updated     TEXT,            -- HA's own last_updated timestamp
    last_error          TEXT
);
```

Timestamps are ISO 8601 UTC with seconds precision (e.g. `2026-10-18T18:48:13+00:00`). On failure only `last_attempt_utc` and `last_error` are updated, so the remaining columns keep describing the last good image / value.

### Media tables

```sql
CREATE TABLE captures (                            -- one row per retained still, so
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,  -- counts/sizes come from the DB
    camera_id           INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    path                TEXT NOT NULL,             -- relative to captures/
    bytes               INTEGER NOT NULL,
    created_utc         TEXT NOT NULL
);

CREATE TABLE review_items (                        -- Frigate alerts & detections
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    server              TEXT NOT NULL,             -- Frigate instance (frigate_host)
    review_id           TEXT NOT NULL,             -- Frigate's unique review id
    camera_name         TEXT NOT NULL,
    camera_id           INTEGER REFERENCES cameras(id) ON DELETE SET NULL,
    severity            TEXT,                       -- 'alert' | 'detection'
    start_time          REAL, end_time REAL,        -- epoch seconds (end NULL while live)
    objects             TEXT,                       -- JSON array of detected objects
    thumb_path          TEXT,
    first_seen_utc      TEXT NOT NULL, updated_utc TEXT NOT NULL,
    clip_status         TEXT NOT NULL DEFAULT 'pending',  -- pending|downloaded|failed|unavailable|deleted|skipped
    clip_path           TEXT, clip_bytes INTEGER, clip_error TEXT,
    retention           TEXT NOT NULL DEFAULT 'new',      -- new|discard|keep7|keep_forever
    retain_until_utc    TEXT, reviewed_utc TEXT,
    UNIQUE (server, review_id)                     -- multi-server safe
);

CREATE TABLE timelapses (                          -- rolling 24h videos, one 'auto' per camera + kept ones
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    server              TEXT NOT NULL,
    camera_id           INTEGER REFERENCES cameras(id) ON DELETE CASCADE,
    camera_name         TEXT NOT NULL,
    created_utc         TEXT NOT NULL,
    covers_start_utc    TEXT, covers_end_utc TEXT,  -- oldest / newest frame
    frame_count         INTEGER,
    path                TEXT, bytes INTEGER,        -- relative to timelapses/
    build_seconds       REAL,                       -- ffmpeg encode time
    retention           TEXT NOT NULL DEFAULT 'auto',  -- auto|keep7|keep_forever
    retain_until_utc    TEXT, reviewed_utc TEXT
);
```

A review item is uniquely keyed by **(server, review_id)**, so several Frigate servers never collide. A camera's clips are served only while `clip_status = 'downloaded'`; a timelapse only while it has a `path`. Every retained still has a `captures` row; a capture's file and row are always deleted together (by the timelapse purge, or the fetcher's keep-newest cleanup for `make_timelapse`-off cameras), so the per-camera count and total size are read straight from the database and stay in sync with disk.

## Frontend behaviour

The monitor has five **modes**, chosen from a top-bar selector (see UI): **Camera** (single), **Grid**, **New** (undecided alerts/detections), **Retained** (kept clips) and **Timelapses**. The review and timelapse modes are shown only when the frontend's `allow_review` / `allow_timelapse` are on.

- **Camera / Grid**: the browser cycles through all enabled cameras in `sort_order`, advancing every `rotation_interval` seconds. For each camera it requests `/api/frame/{id}` (the capture the DB points to) and `/api/status/{id}` in parallel; cameras are addressed by DB `id`, not list position. If a camera has no image yet it skips to the next; if every camera is skipped in one pass an error is shown. A camera whose latest fetch failed still shows its last good image, with the error in the top bar. Grid refreshes all tiles each interval.
- On every tick it re-requests `/api/config`. If the camera list changed (added, removed, renamed, reordered, enabled/disabled) it rebuilds the grid and stays on the current camera if it still exists, otherwise restarts from the first. It also re-polls the New-alert count for the bell badge, in every mode.
- The download button fetches the full-resolution original (no `height`/`quality` parameters) live from Frigate for the camera on screen.
- `/api/stats` is requested on load and every `rotation_interval` seconds, in every mode where stats are shown.
- **New / Retained / Timelapses** share one video player. On entering, the frontend loads the list (`/api/reviews?status=…` or `/api/timelapses`) and auto-plays each clip full-screen, muted, advancing on the video's `ended` event; a progress bar (fill only) tracks playback. Three buttons below the video apply a decision and advance: New = Discard / Keep 7 days / Keep forever; Retained = Discard / Keep forever / Skip; Timelapses = Keep 7 days / Keep forever / Skip. When a queue drains, the next tick re-polls and resumes once new items arrive (so a left-open New view picks up fresh alerts without a reload).

## Configuration

**Global settings** — stored in the database (`settings` table) and edited in the admin page's **Settings** section. There is no config file. `CONFIG_SPEC` in `backend/app/common.py` is the single source of truth for every key's type, default, valid range, admin group and label; the table holds **only overrides**, so an unset key uses its built-in default. `load_config()` returns defaults overlaid with overrides.

| Group | Key | Default | Range | Meaning |
|---|---|---|---|---|
| General | `rotation_interval` | 8 | 1–3600 | Seconds each camera is shown; served via `/api/config` |
| Capture | `fetch_interval` | 60 | 1–3600 | Seconds between capture cycles (every camera) |
| Capture | `fetch_workers` | 8 | 1–64 | Capture thread pool size (**fetcher restart** to apply) |
| Reviews | `review_enabled` | true | — | Poll Frigate for alerts/detections |
| Reviews | `review_interval` | 30 | 5–3600 | Seconds between review polls |
| Reviews | `review_limit` | 50 | 1–500 | Items per poll |
| Reviews | `clip_max_per_cycle` | 6 | 1–100 | Clips downloaded per cycle |
| Reviews | `new_retention_days` | 3 | 1–365 | Undecided clips auto-expire after this |
| Timelapse | `timelapse_enabled` | true | — | Run the timelapse builder |
| Timelapse | `timelapse_interval` | 3600 | 60–86400 | Seconds between builds |
| Timelapse | `timelapse_fps` | 30 | 1–60 | Output frame rate |
| Timelapse | `timelapse_window_hours` | 24 | 1–168 | Rolling window |
| Timelapse | `timelapse_height` | 480 | 120–2160 | Output height (px) |
| Timelapse | `timelapse_min_frames` | 2 | 2–100000 | Minimum captures to build one |

Settings are re-read every cycle by the fetcher and timelapse runner and on every `/api/config` request, so changes apply without a restart (except `fetch_workers`).

**cameras.csv** — **seed only**: imported into the `cameras` table on first start when that table is empty, then ignored. Volume-mounted read-only into both containers; excluded from git and the Docker build context. See `app/cameras-sample.csv`:

```
frigate_host,frigate_port,frigate_camera_name,camera_rename,enabled
frigate.mydns.local,5000,Woodshed_Frigate,Woodshed,TRUE
```

Columns map directly onto the `cameras` table. `enabled` of `false`, `0` or `no` (case-insensitive) imports the camera as disabled; optional `quality` and `height` columns are imported if present (`download_clips` / `make_timelapse` default on and are edited on the admin page). **Frigate resize note:** the downscale parameter is `height=` (Frigate 0.17); setting a camera's `height` therefore limits capture resolution (e.g. 480 → 853×480). Leaving it blank serves full camera resolution and much larger captures.

**Frontend environment** — set on the `frontend` service; rendered into the nginx config at container start:

- `LISTEN_PORT` — port nginx listens on (default `8010`)
- `API_UPSTREAM` — backend base URL that `/api/*` is proxied to (default `http://api:8011`, the compose service name)
- `FRONTEND_NAME` — which backend frontend this container serves (default `main`); must match a row in the `frontends` table

**.env** — Home Assistant connection, as environment variables. Lives at the repo root next to `docker-compose.yml`; excluded from git and the Docker build context. Loaded into the `fetcher` container only (`env_file`, `required: false`), so the token is never visible to the API, the frontend or the browser. See `.env.example`:

```
HA_URL=http://homeassistant.local:8123
HA_TOKEN=<long-lived access token>
```

- `HA_URL` — Home Assistant base URL (trailing `/` ignored)
- `HA_TOKEN` — long-lived access token (HA profile → Security)

**ha-entities.csv** — **seed only**: imported into the `ha_entities` table on first start when that table is empty, then ignored. Committed to git (no secrets) and volume-mounted read-only into both containers:

```
order,label,entity_id,decimals,unit,scale
1,Outside,sensor.bonaparte_temp,0,°C,1
4,Battery SOC,sensor.lxp_battery_lxp_battery_soc,0,%,1
5,Battery Net,sensor.lxp_battery_net_power,2,kW,0.001
```

### HA entity fields

These apply to the `ha_entities` table (as edited on the admin page) and to the seed CSV:

- `order` / `sort_order` — position in the top bar, left to right (ascending; decimals allowed). Blank sorts last.
- `label` — text shown above the value (required)
- `entity_id` — HA entity to display; leave blank for a placeholder slot showing "—"
- `decimals` — optional (0–6); numeric states are rounded to this many places (after scaling / unit conversion)
- `unit` — optional display unit. If blank, HA's `unit_of_measurement` is shown. If it differs from HA's unit, the API converts the value: supported pairs are `W`↔`kW`, `Wh`↔`kWh` and `°F`↔`°C`. Unsupported pairs or non-numeric states fall back to HA's own value and unit, so a value is never shown under the wrong label. Use the exact symbols `%`, `°C`, `kW`.
- `scale` — optional multiplier applied to numeric states (e.g. `0.001` for W → kW). When set, automatic unit conversion is skipped and the value is labelled with `unit` as written (or HA's unit if `unit` is blank). Applied before `decimals` rounding; non-numeric states are shown unscaled.

**Environment** — `DATA_DIR` overrides the data directory (default: `../data` relative to the app directory, which is `/data` in the container).

## Stack

- **Backend**: Python 3.12; fetcher uses `httpx.Client` + `ThreadPoolExecutor`; API uses FastAPI, uvicorn, `httpx.AsyncClient` and Pydantic v2 validation
- **Storage**: local filesystem for captures/clips/timelapses, SQLite for configuration and records, on the `camera-data` Docker named volume
- **Media**: ffmpeg (in the backend image) for timelapse encoding (`libx264`, CPU; GPU planned)
- **Frontend**: nginx 1.27 (alpine) serving one static HTML/JS page with no build step (`frontend/public/index.html`), which calls the API with relative `/api/...` URLs.
- **Admin page**: a static HTML/JS page in the backend (`backend/app/static/admin.html`), served by FastAPI and calling the admin API on the same origin.

## Deployment

- A single `docker-compose.yml` works on both a Linux host and Docker Desktop for Mac: `docker compose up -d --build`. The backend image is published to GHCR by GitHub Actions and pulled by Portainer; `GHCR_OWNER` selects the registry path (`local` for a local build).
- Services run on the default compose network (no host networking). The `frontend` publishes **8010** and `api` publishes **8011**; `fetcher` and `timelapse` publish nothing. Frontends reach the API by service name (`API_UPSTREAM=http://api:8011`).
- To serve another audience, add a frontend on the admin page, tick its cameras, set its options (e.g. Review/Timelapse off for public), then run another frontend container with a different `FRONTEND_NAME` and port. `docker-compose.yml` defines a `frontend-public` on port 8020.
- The fetcher and API reach Frigate and Home Assistant by hostname through Docker's DNS, which forwards to the host's resolver. The `.lcars` names must resolve from inside containers.
- The deployment sits local to the viewing location, so frames do not round-trip over Starlink.

## HTTP API (backend)

Served by the `api` service on port 8011. The non-admin endpoints are also reachable through the frontend at the same paths. Besides `/api/*`, the backend serves `/` (redirects to `/admin`), `/admin` (admin page) and FastAPI's `/docs` (interactive API docs).

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | `{"status": "ok"}` once the DB is reachable (no header needed) |

**Monitor endpoints are scoped to a frontend.** They require the `X-Frontend-Name` header (normally added by the frontend's nginx): missing → `400`, unknown name → `404`. A camera that is disabled or not mapped to the frontend returns `404`, exactly like a non-existent one.

| Method | Path | Description |
|---|---|---|
| GET | `/api/config` | `{"frontend", "cameras": [{"id", "name"}], "rotation_interval", "show_stats", "allow_download", "allow_review", "allow_timelapse"}`: enabled cameras mapped to this frontend, in `sort_order`. `name` is `camera_rename` |
| GET | `/api/frame/{id}` | The capture `fetches.latest_image` points to. `404` if not in this frontend, `503` if none saved yet |
| GET | `/api/status/{id}` | `{last_attempt_utc, last_fetched_utc, payload_bytes, last_error}` from SQLite (all `null` if never attempted). If the frontend's `show_errors` is off, `last_error` is replaced with `"Camera unavailable"` |
| GET | `/api/stats` | `{"stats": [{label, entity_id, decimals, state, unit, last_fetched_utc, last_error}]}`, one per `ha_entities` row in `sort_order`; values from `ha_states` (`null` if never fetched), with `state`/`unit` already scaled / converted. Empty list if the frontend's `show_stats` is off. With `show_errors` off, `entity_id` is `null` and errors read `"Unavailable"` |
| GET | `/api/frame/{id}/original` | Full-resolution JPEG fetched live from Frigate (10 s timeout, not stored), sent as an attachment named `<frigate_camera_name>.jpg`. `403` if the frontend's `allow_download` is off; `503` on failure (generic message if `show_errors` is off) |

**Review & timelapse endpoints** are likewise frontend-scoped (only cameras mapped to the frontend), and return `403` when the frontend's `allow_review` / `allow_timelapse` is off. Clip/video responses are `FileResponse` and honour `Range` for seeking.

| Method | Path | Description |
|---|---|---|
| GET | `/api/reviews?status=new\|retained` | Review items with a downloaded clip, `new` = undecided, `retained` = kept; each `{id, camera, severity, start_time, end_time, duration, objects, retention, clip_url}` |
| GET | `/api/reviews/{id}/clip.mp4` | The stored clip. `404` if not downloaded / not in this frontend |
| POST | `/api/reviews/{id}/retention` | Body `{retention: discard\|keep7\|keep_forever}`; `keep7` sets a 7-day expiry |
| GET | `/api/timelapses` | Timelapses for this frontend's cameras, newest first; each `{id, camera, created_utc, covers_start_utc, covers_end_utc, frame_count, retention, clip_url}` |
| GET | `/api/timelapses/{id}/video.mp4` | The compiled video (inline for playback). With `?download=1` it is sent as an attachment named `<instance>_<camera>_<yyyymmdd>_<hhmmss>.mp4`; `403` if the frontend's `allow_download` is off |
| POST | `/api/timelapses/{id}/retention` | Body `{retention: auto\|keep7\|keep_forever}` |

### Admin API

Backend port only: the frontend returns `403` for `/api/admin/*`. No authentication. Request bodies are validated; invalid input returns `422` with per-field messages.

| Method | Path | Description |
|---|---|---|
| GET | `/api/admin/cameras` | All cameras (including disabled) in `sort_order`, each with `frontend_ids`, `last_fetched_utc`/`last_error` from `fetches`, and the metrics `capture_count`, `capture_bytes`, `timelapse_count`, `timelapse_bytes`, `last_timelapse_seconds`, `last_timelapse_utc` |
| POST | `/api/admin/cameras` | Create. Body: `sort_order?`, `frigate_host`, `frigate_port` (1–65535, default 5000), `frigate_camera_name`, `camera_rename`, `enabled` (default true), `quality?` (1–100), `height?` (≥1), `download_clips` (default true), `make_timelapse` (default true), `frontend_ids?` (list; omitted = leave mapping unchanged, unknown ids ignored). `409` if `frigate_host` + `frigate_camera_name` already exists. Returns `{id}` |
| PUT | `/api/admin/cameras/{id}` | Replace all fields (same body). `404` / `409` as applicable |
| DELETE | `/api/admin/cameras/{id}` | Delete the camera, its `fetches` row and its image file. `204` |
| GET | `/api/admin/ha-entities` | All entities in `sort_order`, each with HA's raw `state`, `ha_unit` and `last_error` from `ha_states` |
| POST | `/api/admin/ha-entities` | Create. Body: `sort_order?`, `label`, `entity_id` (default blank), `decimals?` (0–6), `unit` (default blank), `scale?`. Returns `{id}` |
| PUT | `/api/admin/ha-entities/{id}` | Replace all fields (same body) |
| DELETE | `/api/admin/ha-entities/{id}` | Delete. `204` |
| GET | `/api/admin/frontends` | All frontends by name, each with `camera_count` (enabled mapped cameras) |
| POST | `/api/admin/frontends` | Create. Body: `name` (letters, digits, `_`, `-`; unique), `show_stats`, `allow_download`, `show_errors`, `allow_review`, `allow_timelapse` (all default true). Returns `{id}` |
| PUT | `/api/admin/frontends/{id}` | Replace all fields (same body). Renaming breaks any container still using the old `FRONTEND_NAME` |
| DELETE | `/api/admin/frontends/{id}` | Delete the frontend and its camera mappings. `204` |
| GET | `/api/admin/settings` | Every setting from `CONFIG_SPEC` with `{key, group, label, type, value, default, min, max, help}` (value = override or default) |
| PUT | `/api/admin/settings` | Body `{"<key>": <value>, …}`. Unknown keys, wrong types or out-of-range values → `422`; valid ones are upserted as overrides. Returns the updated list |

## Frontend routes

| Path | Served |
|---|---|
| `/` | `index.html` (`Cache-Control: no-store`) |
| `/api/admin/*` | `403` (admin is backend-only) |
| `/api/*` | Proxied to `API_UPSTREAM` |
| anything else | Static file from `public/` or `404` |

## UI

The page is a vertical stack: a fixed top bar, with the camera area filling the remaining height below it. Nothing overlays the camera image.

- **Top bar** (dark, full width, min height 3.75rem), left to right:
  - **Camera info**: the camera's display name with an icon-only download button (down arrow) beside it (hidden if the frontend's `allow_download` is off), "Fetched <`last_fetched_utc` in browser local time>", and, if the most recent fetch errored, "Last attempt failed: …" in red (truncated, full text on hover)
  - **Stats**: one evenly spaced label/value slot per `ha_entities` row, left to right by `sort_order`. The whole stats area is hidden if the frontend's `show_stats` is off. Values show as `<state> <unit>` (`%` without a space), rounded to `decimals` if set. "—" is shown when there is no entity, no value yet, or the state is `unavailable`/`unknown`. If the latest fetch failed, the last good value is shown greyed out, with the error on hover; otherwise hovering shows the entity ID.
  - **Mode selector** (far right): chromeless icon-only buttons — camcorder (Camera), grid (Grid), bell (New, with a red count badge of undecided alerts), bookmark (Retained), film strip (Timelapses). Dim when inactive, bright with a blue underline when active; full labels as tooltips. New/Retained appear only with `allow_review`; Timelapses only with `allow_timelapse`.
- **Download button**: in Camera mode downloads the original frame from `/api/frame/{id}/original` (saved as `<display name>.jpg`); in Timelapses mode downloads the playing video from `/api/timelapses/{id}/video.mp4?download=1` (saved as `<instance>_<camera>_<timestamp>.mp4`). Hidden in Grid/New/Retained, and whenever the frontend's `allow_download` is off.
- **Camera area** (Camera mode): the single-camera image is scaled to the full width of the screen and vertically centred; any excess height is cropped equally from the top and bottom. Camera info updates only once the new image has loaded. Tapping the image switches to Grid.
- **Grid mode**: a grid of every camera (`ceil(√n)` columns, each tile `object-fit: contain`) with each `camera_rename` top-right. The top bar stays visible ("All Cameras", fetch time/error/download hidden). Rotation pauses; every `rotation_interval` seconds all tiles re-fetch in parallel. A tile with no image shows "No image"; a failed refresh keeps the last. Tapping a tile returns to Camera mode on it.
- **Review & timelapse modes** (New / Retained / Timelapses): the camera area is replaced by a muted, full-screen video with the top bar still in place; the camera-info area shows the clip's camera, metadata (alert: `SEVERITY · objects · duration · time`; timelapse: `TIMELAPSE · N frames · to <time> · KEPT…`) and `n of N`. A bottom progress bar (fill only, not seekable) tracks playback. Clips auto-play one after another; the three retention buttons apply a decision and advance. An empty queue shows a message and the next tick re-polls for new items. Playback is **double-buffered** — two stacked `<video>` elements, the next clip preloaded in the idle one and revealed on advance — so auto-advance is seamless (no empty/paused frame, which on Android would otherwise flash the native play-arrow overlay between clips).
- A red-text error overlay covers the camera area, and the image is hidden, when:
  - `/api/config` fails: "Failed to load camera configuration: <reason>" (e.g. `Unknown frontend 'x'` when `FRONTEND_NAME` has no matching row)
  - no cameras are returned: "No cameras are configured."
  - no camera has an image available: "No cameras are currently available."
- No authentication required.

### Admin page (backend, `http://<host>:8011/admin`)

- Served by the backend, not the frontend; the monitor has no link to it. Same dark styling as the monitor; the header links to the API docs (`/docs`).
- A sticky header holds the unsaved-change count, **Revert** and **Save**. There are no per-row save buttons.
- A **Settings** section (below Frontends) shows every global setting grouped into General / Capture / Reviews / Timelapse, as number inputs (with min/max) or checkboxes; hovering a label shows its help and default. Setting edits are staged like table edits (amber label, counted in the unsaved total) and sent with the same **Save** as one `PUT /api/admin/settings`; **Revert** restores them.
- Three editable tables: **Cameras** and **Home Assistant Stats** (one row per DB row in `sort_order`) and **Frontends** (by name: name, Stats, Download, Errors, Review, Timelapse checkboxes, Status showing the enabled camera count). Inputs per field, checkboxes for booleans.
- The Cameras table has **On**, **Clips** (`download_clips`) and **T-lapse** (`make_timelapse`) checkboxes, then one checkbox column per saved frontend (labelled with the frontend's name) choosing which frontends show each camera. A frontend added on the page gets its column after saving. New cameras start unticked for every frontend. Tables scroll horizontally on narrow screens.
- All edits are **staged** until Save:
  - Editing a field marks the row changed (amber). A row edited back to its saved values is no longer counted.
  - **Delete** marks an existing row for deletion (red, struck through, inputs disabled); the button becomes **Undo**.
  - **+ Add camera** / **+ Add stat** / **+ Add frontend** appends a new row (green) whose **Remove** button discards it. New rows left untouched (defaults only) are ignored.
- **Save** sends all staged changes: deletes first (so a camera can be deleted and re-added in one save), then updates, then creates. If everything succeeds the tables reload from the server. If anything fails, successful rows are committed, and each failed row stays staged with a red marker and the API's error in its Status cell. A toast summarises (`Saved N, M failed: …`) so the row can be fixed and saved again.
- **Revert** (after a confirmation) discards all staged changes and reloads both tables.
- Leaving the page with unsaved changes triggers the browser's "leave site?" prompt.
- A read-only **Status** column shows, for cameras: line 1 "OK · <last fetched time>", the latest fetch error in red (full text on hover), "Not fetched yet" or "Disabled"; line 2 the per-camera metrics "`N imgs X MB · K t-lapses Y MB · built Zs`" (captures on disk and their size, timelapse videos on disk and their size, and the most recent timelapse's build time — all from the DB). A totals line under the Cameras table sums these across all cameras ("Total: N cameras (M enabled) · X images A MB · K timelapses B MB · A+B MB on disk"). For HA entities: HA's raw value and unit (`HA: 1049.0 W`), the latest error in red, "Placeholder" or "Not fetched yet". New rows show "New"; rows just saved show "Saved".

## Known Limitations

- Images are never marked stale. A camera that keeps failing shows its last good image indefinitely; only the Last Fetched time and error text reveal this.
- **The backend trusts `X-Frontend-Name`.** Frontend isolation depends on port 8011 being private and on each frontend's nginx overwriting the header. Authentication for an audience belongs in front of its frontend container (reverse proxy, VPN or access gateway).
- **The API, admin endpoints and admin page have no authentication, by design.** Anyone who can reach port 8011 can add, change or delete cameras and stats. Frontend ports are read-only and limited to their mapped cameras. Keep 8011 on the private network.
- Changing `.env` needs `docker compose up -d` (a plain restart does not reload `env_file`).- Changing a camera's `frigate_host` or `frigate_camera_name` leaves its old image file and `fetches` row behind (deleting the camera does clean up).
- Edits to `cameras.csv` / `ha-entities.csv` have no effect once the tables are seeded. To re-seed, delete the rows on the admin page (or remove the DB) and restart.
- Changing `fetch_workers` needs a fetcher restart (the thread pool is sized at startup); all other settings apply within a cycle.
- **Retained captures are the main storage cost**: at 480p / 60 s that is ~1440 files/camera/day (purged at 24h). Clips and timelapses are pulled/encoded on top. Use the per-camera **Clips**/**T-lapse** toggles and `height`/`fetch_interval` to manage volume on a low-capacity host.
- **Clip downloads cross Starlink.** Each alert's clip is pulled from the remote Frigate at record resolution; it downloads both alerts and detections. Raise `review_interval` / lower `clip_max_per_cycle`, or turn `download_clips` off per camera, to reduce traffic.
- Timelapse encoding is CPU-only for now; a dense 24h window over many cameras is a periodic CPU spike each hour.
- A timelapse build is skipped (logged) if a camera has fewer than `timelapse_min_frames` captures in the window.
