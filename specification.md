# Camera Monitor — Specification

## Purpose

A lightweight web app that rotates through a list of cameras, displaying a single still frame per camera per rotation. The goal is to monitor remote cameras over a low-bandwidth Starlink connection (~20–30 Mbps upload) without the overhead of live streaming relatively static scenes.

Frames are sourced from an existing [Frigate](https://frigate.video/) NVR instance via its HTTP API (`/api/<camera>/latest.jpg`), rather than by connecting to cameras directly. Frigate can downscale and recompress the snapshot server-side, keeping the per-frame payload small.

Site stats (temperatures, battery, inverter) shown in the top bar are read from [Home Assistant](https://www.home-assistant.io/) via its REST API.

## Architecture

Two independent apps, each with its own directory, Dockerfile and image:

| App | Directory | Image | Contents |
|---|---|---|---|
| **Backend** | `backend/` | `camera-monitor-backend` | Python: the fetcher, the JSON/image API, and the admin page (`/admin`). |
| **Frontend** | `frontend/` | `camera-monitor-frontend` | nginx serving the read-only monitor page and proxying `/api/*` (except `/api/admin/*`) to the backend. No Python. |

They run as three containers:

| Service | Image | Command | Role |
|---|---|---|---|
| `fetcher` | backend | `python fetcher.py` | Polls Frigate and Home Assistant on a fixed schedule; writes images to disk and fetch records / HA states to SQLite. The only service with the HA token |
| `api` | backend | `uvicorn main:app --port 8011` | **Unauthenticated** JSON/image API and admin page (intended for a private network). Reads images and fetch records written by the fetcher; reads/writes camera and HA entity configuration. Only contacts Frigate for the original-image download |
| `frontend` | frontend | nginx on port **8010** | Serves `index.html` (monitor); forwards `/api/*` to `API_UPSTREAM`, so the browser only ever talks to the frontend origin (no CORS needed). Refuses `/api/admin/*` with `403`, so configuration can only be changed on the backend port |

`fetcher` and `api` share the `camera-data` volume mounted at `/data`. The frontend has no access to it.

```
Frigate ─────────HTTP──▶ fetcher ──▶ /data/images/*.jpg
Home Assistant ──HTTP──▶    │    └──▶ /data/camera-monitor.db
                                          ▲
                                    api :8011
                                          ▲  /api/*
                    browser ──▶ frontend (nginx) :8010     (monitor)
             admin browser ──▶ api :8011/admin               (configuration)
```

Camera and Home Assistant entity configuration lives in the same SQLite database, edited through the backend's admin page (`http://<host>:8011/admin`). Shared backend code (DB schema and seeding, config queries, Frigate URL building, image paths) lives in `backend/app/common.py`.

### Repository layout

```
backend/
  Dockerfile              python:3.12-slim; default CMD runs the API on 8011
  requirements.txt
  app/
    main.py               API (FastAPI)
    fetcher.py            fetcher loop
    common.py             DB schema, seeding, shared helpers
    static/admin.html     admin page (served by the API at /admin)
    config.csv            global settings (baked into the image)
    cameras-sample.csv    example seed file
    cameras.csv           seed for the cameras table (gitignored, volume-mounted)
    ha-entities.csv       seed for the ha_entities table (volume-mounted)
frontend/
  Dockerfile              nginx:1.27-alpine
  nginx/default.conf.template
  public/
    index.html            monitor
docker-compose.yml        all three services (Linux and Docker Desktop)
.env / .env.example       HA_URL / HA_TOKEN for the fetcher
```

## Fetcher

- At startup, creates the DB tables and seeds configuration if empty (see Database).
- At the start of every cycle, re-reads enabled cameras and HA entities from the DB, so admin-page edits take effect on the next cycle without a restart.
- Every `fetch_interval` seconds (default **15**), fetches all enabled cameras in parallel using a thread pool of `fetch_workers` threads (default **8**).
- Each fetch is a single attempt with a 10-second timeout. There are no in-cycle retries; the next cycle retries.
- On success, writes the JPEG to `/data/images/<frigate_host>__<frigate_camera_name>.jpg`, overwriting the previous version. Characters other than `A-Z a-z 0-9 . _ -` become `_`. The file is written to a `.tmp` sibling and atomically renamed, so readers never see a partial image.
- After each cycle, results are written to SQLite in one transaction.
- The next cycle starts `fetch_interval` seconds after the previous one *started*. If a cycle overruns the interval, the next one starts immediately.
- **Home Assistant**: in the same cycle and thread pool, fetches `GET <url>/api/states/<entity_id>` (header `Authorization: Bearer <token>`, 10 s timeout) for each distinct non-empty `entity_id` in the `ha_entities` table, storing `state`, `attributes.unit_of_measurement` and `last_updated`. If `HA_URL` or `HA_TOKEN` is unset or empty, HA polling is skipped (logged once at startup); camera fetching is unaffected.
- Failures are logged as warnings; each cycle logs a summary (`cameras N ok, M failed; HA N ok, M failed`).

## Database

SQLite at `/data/camera-monitor.db` (WAL mode).

### Configuration tables

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
    height              INTEGER,                 -- Frigate h=
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

**Seeding**: at startup both services run `init_db()`, which creates any missing tables and, under a `BEGIN IMMEDIATE` write lock (so it happens once even when both start together), imports `cameras.csv` into `cameras` if that table is empty and `ha-entities.csv` into `ha_entities` if that table is empty. CSV row order becomes `sort_order` (1, 2, 3…), except that `ha-entities.csv`'s own `order` column is used when present. Once a table has rows, its CSV is ignored.

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

## Frontend behaviour

- The browser cycles through all enabled cameras in `sort_order`, looping continuously, advancing every `rotation_interval` seconds.
- For each camera it requests `/api/frame/{id}` (the image on disk) and `/api/status/{id}` (the DB record) in parallel. Cameras are addressed by DB `id`, not list position.
- On every tick it re-requests `/api/config`. If the camera list changed (added, removed, renamed, reordered, enabled/disabled), it rebuilds the grid and stays on the current camera if it still exists, otherwise restarts from the first.
- If a camera has no image on disk yet, the browser immediately skips to the next camera. If every camera is skipped in one pass, an error is shown. A camera whose latest fetch failed still shows its last good image, with the error displayed in the top bar.
- The download button fetches the full-resolution original (no `quality`/`h` parameters) live from Frigate for the camera on screen.
- `/api/stats` is requested on page load and then every `rotation_interval` seconds, in both single and grid mode.

## Configuration

**config.csv** — global backend settings, **baked into the backend image** at build time (`backend/app/config.csv` → `/app/config.csv`):

```
key,value
rotation_interval,10
fetch_interval,15
fetch_workers,8
```

- `rotation_interval` — seconds each camera is displayed in the UI (default `10`); served to the frontend via `/api/config`
- `fetch_interval` — seconds between fetcher cycles (default `15`)
- `fetch_workers` — fetcher thread pool size (default `8`)

**cameras.csv** — **seed only**: imported into the `cameras` table on first start when that table is empty, then ignored. Volume-mounted read-only into both containers; excluded from git and the Docker build context. See `app/cameras-sample.csv`:

```
frigate_host,frigate_port,frigate_camera_name,camera_rename,enabled
frigate.mydns.local,5000,Woodshed_Frigate,Woodshed,TRUE
```

Columns map directly onto the `cameras` table. `enabled` of `false`, `0` or `no` (case-insensitive) imports the camera as disabled; optional `quality` and `height` columns are imported if present.

**Frontend environment** — set on the `frontend` service; rendered into the nginx config at container start:

- `LISTEN_PORT` — port nginx listens on (default `8010`)
- `API_UPSTREAM` — backend base URL that `/api/*` is proxied to (default `http://api:8011`, the compose service name)

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
- **Storage**: local filesystem for images, SQLite for configuration and fetch records, on the `camera-data` Docker named volume
- **Frontend**: nginx 1.27 (alpine) serving one static HTML/JS page with no build step (`frontend/public/index.html`), which calls the API with relative `/api/...` URLs.
- **Admin page**: a static HTML/JS page in the backend (`backend/app/static/admin.html`), served by FastAPI and calling the admin API on the same origin.

## Deployment

- A single `docker-compose.yml` works on both a Linux host and Docker Desktop for Mac: `docker compose up -d --build`.
- All three services run on the default compose network (no host networking). The frontend publishes **8010** and the API publishes **8011**; the fetcher publishes nothing. The frontend reaches the API by service name (`API_UPSTREAM=http://api:8011`).
- The fetcher and API reach Frigate and Home Assistant by hostname through Docker's DNS, which forwards to the host's resolver. The `.lcars` names must resolve from inside containers.
- The deployment sits local to the viewing location, so frames do not round-trip over Starlink.

## HTTP API (backend)

Served by the `api` service on port 8011. The non-admin endpoints are also reachable through the frontend at the same paths. Besides `/api/*`, the backend serves `/` (redirects to `/admin`), `/admin` (admin page) and FastAPI's `/docs` (interactive API docs).

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | `{"status": "ok"}` once the DB is reachable |
| GET | `/api/config` | `{"cameras": [{"id", "name"}], "rotation_interval"}`, enabled cameras in `sort_order`. `name` is `camera_rename` |
| GET | `/api/frame/{id}` | Latest JPEG on disk for the camera. `404` for an unknown id, `503` if the fetcher has not saved one yet |
| GET | `/api/status/{id}` | `{last_attempt_utc, last_fetched_utc, payload_bytes, last_error}` from SQLite (all `null` if never attempted). `404` for an unknown id |
| GET | `/api/stats` | `{"stats": [{label, entity_id, decimals, state, unit, last_fetched_utc, last_error}]}`, one per `ha_entities` row in `sort_order`; values from `ha_states` (`null` if never fetched), with `state`/`unit` already scaled / converted to the configured `unit` |
| GET | `/api/frame/{id}/original` | Full-resolution JPEG fetched live from Frigate (10 s timeout, not stored), sent as an attachment named `<frigate_camera_name>.jpg`. `503` on failure |

### Admin API

Backend port only: the frontend returns `403` for `/api/admin/*`. No authentication. Request bodies are validated; invalid input returns `422` with per-field messages.

| Method | Path | Description |
|---|---|---|
| GET | `/api/admin/cameras` | All cameras (including disabled) in `sort_order`, each with `last_fetched_utc` and `last_error` from `fetches` |
| POST | `/api/admin/cameras` | Create. Body: `sort_order?`, `frigate_host`, `frigate_port` (1–65535, default 5000), `frigate_camera_name`, `camera_rename`, `enabled` (default true), `quality?` (1–100), `height?` (≥1). `409` if `frigate_host` + `frigate_camera_name` already exists. Returns `{id}` |
| PUT | `/api/admin/cameras/{id}` | Replace all fields (same body). `404` / `409` as applicable |
| DELETE | `/api/admin/cameras/{id}` | Delete the camera, its `fetches` row and its image file. `204` |
| GET | `/api/admin/ha-entities` | All entities in `sort_order`, each with HA's raw `state`, `ha_unit` and `last_error` from `ha_states` |
| POST | `/api/admin/ha-entities` | Create. Body: `sort_order?`, `label`, `entity_id` (default blank), `decimals?` (0–6), `unit` (default blank), `scale?`. Returns `{id}` |
| PUT | `/api/admin/ha-entities/{id}` | Replace all fields (same body) |
| DELETE | `/api/admin/ha-entities/{id}` | Delete. `204` |

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
  - **Camera info**: the camera's display name with an icon-only download button (down arrow) beside it, "Fetched <`last_fetched_utc` in browser local time>", and, if the most recent fetch errored, "Last attempt failed: …" in red (truncated, full text on hover)
  - **Stats**: one evenly spaced label/value slot per `ha_entities` row, left to right by `sort_order`. Values show as `<state> <unit>` (`%` without a space), rounded to `decimals` if set. "—" is shown when there is no entity, no value yet, or the state is `unavailable`/`unknown`. If the latest fetch failed, the last good value is shown greyed out, with the error on hover; otherwise hovering shows the entity ID.
- **Download button**: downloads the original frame from `/api/frame/{id}/original` (saved as `<display name>.jpg`); disabled while in progress; failures are silent.
- **Camera area**: the single-camera image is scaled to the full width of the screen and vertically centred; any excess height is cropped equally from the top and bottom (black bars appear above and below if the image is shorter than the area).
- Camera info updates only once the new image has loaded.
- **Grid mode**: tapping the single-camera image replaces the camera area with a grid of every camera (`ceil(√n)` columns, each tile `object-fit: contain`), with each camera's `camera_rename` in the tile's top-right corner. The top bar stays visible, with "All Cameras" as the name and the fetch time, error and download button hidden. Tiles are letterboxed (`object-fit: contain`), not cropped. Rotation pauses; every `rotation_interval` seconds all tiles re-fetch `/api/frame/{id}` in parallel. A tile with no image yet shows "No image"; a failed refresh keeps the tile's last image. Tapping a tile returns to single-camera mode on that camera, and rotation resumes from there.
- A red-text error overlay covers the camera area, and the image is hidden, when:
  - `/api/config` fails: "Failed to load camera configuration."
  - no cameras are returned: "No cameras are configured."
  - no camera has an image available: "No cameras are currently available."
- No authentication required.

### Admin page (backend, `http://<host>:8011/admin`)

- Served by the backend, not the frontend; the monitor has no link to it. Same dark styling as the monitor; the header links to the API docs (`/docs`).
- A sticky header holds the unsaved-change count, **Revert** and **Save**. There are no per-row save buttons.
- Two editable tables, **Cameras** and **Home Assistant Stats**, one row per DB row in `sort_order`, with an input per field (checkbox for `enabled`). Tables scroll horizontally on narrow screens.
- All edits are **staged** until Save:
  - Editing a field marks the row changed (amber). A row edited back to its saved values is no longer counted.
  - **Delete** marks an existing row for deletion (red, struck through, inputs disabled); the button becomes **Undo**.
  - **+ Add camera** / **+ Add stat** appends a new row (green) whose **Remove** button discards it. New rows left untouched (defaults only) are ignored.
- **Save** sends all staged changes: deletes first (so a camera can be deleted and re-added in one save), then updates, then creates. If everything succeeds the tables reload from the server. If anything fails, successful rows are committed, and each failed row stays staged with a red marker and the API's error in its Status cell. A toast summarises (`Saved N, M failed: …`) so the row can be fixed and saved again.
- **Revert** (after a confirmation) discards all staged changes and reloads both tables.
- Leaving the page with unsaved changes triggers the browser's "leave site?" prompt.
- A read-only **Status** column shows, for cameras: "OK · <last fetched time>", the latest fetch error in red (full text on hover), "Not fetched yet" or "Disabled"; for HA entities: HA's raw value and unit (`HA: 1049.0 W`), the latest error in red, "Placeholder" or "Not fetched yet". New rows show "New"; rows just saved show "Saved".

## Known Limitations

- Images are never marked stale. A camera that keeps failing shows its last good image indefinitely; only the Last Fetched time and error text reveal this.
- **The API, admin endpoints and admin page have no authentication, by design.** Anyone who can reach port 8011 can add, change or delete cameras and stats. Port 8010 (frontend) is read-only. Keep 8011 on the private network.
- Changing `.env` needs `docker compose up -d` (a plain restart does not reload `env_file`). Changing `config.csv` needs a backend image rebuild.
- Changing a camera's `frigate_host` or `frigate_camera_name` leaves its old image file and `fetches` row behind (deleting the camera does clean up).
- Edits to `cameras.csv` / `ha-entities.csv` have no effect once the tables are seeded. To re-seed, delete the rows on the admin page (or remove the DB) and restart.
- `config.csv` settings (`rotation_interval`, `fetch_interval`, `fetch_workers`) are not yet in the DB or admin page.
