from __future__ import annotations

import csv
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
CAMERAS_CSV = BASE_DIR / "cameras.csv"
CONFIG_CSV = BASE_DIR / "config.csv"
HA_ENTITIES_CSV = BASE_DIR / "ha-entities.csv"
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR.parent / "data"))
IMAGES_DIR = DATA_DIR / "images"
CLIPS_DIR = DATA_DIR / "clips"
CAPTURES_DIR = DATA_DIR / "captures"      # retained timestamped stills (per camera)
TIMELAPSE_DIR = DATA_DIR / "timelapses"   # compiled timelapse videos (per camera)
DB_PATH = DATA_DIR / "camera-monitor.db"

# Retention states for a review item (alert/detection)
RETAIN_NEW = "new"              # undecided; shown in the "New" review mode
RETAIN_DISCARD = "discard"      # user discarded; clip deleted
RETAIN_7D = "keep7"             # keep for 7 days
RETAIN_FOREVER = "keep_forever"
RETENTIONS = (RETAIN_NEW, RETAIN_DISCARD, RETAIN_7D, RETAIN_FOREVER)

# Retention states for a timelapse: 'auto' is the rolling current one, replaced
# each hour unless the user elects to keep it (same keep7/keep_forever mechanism).
TL_AUTO = "auto"
TL_7D = "keep7"
TL_FOREVER = "keep_forever"
TL_RETENTIONS = (TL_AUTO, TL_7D, TL_FOREVER)

SCHEMA = """
CREATE TABLE IF NOT EXISTS fetches (
    frigate_host        TEXT NOT NULL,
    frigate_camera_name TEXT NOT NULL,
    last_attempt_utc    TEXT NOT NULL,
    last_fetched_utc    TEXT,
    payload_bytes       INTEGER,
    last_error          TEXT,
    latest_image        TEXT,                 -- newest capture, relative to CAPTURES_DIR
    PRIMARY KEY (frigate_host, frigate_camera_name)
);

CREATE TABLE IF NOT EXISTS ha_states (
    entity_id           TEXT PRIMARY KEY,
    last_attempt_utc    TEXT NOT NULL,
    last_fetched_utc    TEXT,
    state               TEXT,
    unit                TEXT,
    ha_last_updated     TEXT,
    last_error          TEXT
);

CREATE TABLE IF NOT EXISTS cameras (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sort_order          REAL,
    frigate_host        TEXT NOT NULL,
    frigate_port        INTEGER NOT NULL DEFAULT 5000,
    frigate_camera_name TEXT NOT NULL,
    camera_rename       TEXT NOT NULL,
    enabled             INTEGER NOT NULL DEFAULT 1,
    quality             INTEGER,
    height              INTEGER,
    download_clips      INTEGER NOT NULL DEFAULT 1,  -- download alert/detection clips?
    make_timelapse      INTEGER NOT NULL DEFAULT 1,  -- retain captures + build a timelapse?
    UNIQUE (frigate_host, frigate_camera_name)
);

CREATE TABLE IF NOT EXISTS ha_entities (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sort_order          REAL,
    label               TEXT NOT NULL,
    entity_id           TEXT NOT NULL DEFAULT '',
    decimals            INTEGER,
    unit                TEXT NOT NULL DEFAULT '',
    scale               REAL
);

-- A frontend container identifies itself with the X-Frontend-Name header
CREATE TABLE IF NOT EXISTS frontends (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL UNIQUE,
    show_stats          INTEGER NOT NULL DEFAULT 1,
    allow_download      INTEGER NOT NULL DEFAULT 1,
    show_errors         INTEGER NOT NULL DEFAULT 1,
    allow_review        INTEGER NOT NULL DEFAULT 1,
    allow_timelapse     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS frontend_cameras (
    frontend_id         INTEGER NOT NULL REFERENCES frontends(id) ON DELETE CASCADE,
    camera_id           INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    PRIMARY KEY (frontend_id, camera_id)
);

-- Frigate review items (alerts and detections). Uniquely keyed by the Frigate
-- server instance plus Frigate's own review id, so several servers can coexist.
CREATE TABLE IF NOT EXISTS review_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    server              TEXT NOT NULL,        -- Frigate server instance (frigate_host)
    review_id           TEXT NOT NULL,        -- Frigate's unique review id
    camera_name         TEXT NOT NULL,        -- Frigate camera name
    camera_id           INTEGER REFERENCES cameras(id) ON DELETE SET NULL,
    severity            TEXT,                 -- 'alert' | 'detection'
    start_time          REAL,                 -- epoch seconds
    end_time            REAL,                 -- epoch seconds (NULL while in progress)
    objects             TEXT,                 -- JSON array of detected objects
    thumb_path          TEXT,                 -- Frigate thumbnail path
    first_seen_utc      TEXT NOT NULL,        -- when this row was first logged
    updated_utc         TEXT NOT NULL,
    clip_status         TEXT NOT NULL DEFAULT 'pending',  -- pending|downloaded|failed|unavailable|deleted|skipped
    clip_path           TEXT,                 -- path relative to CLIPS_DIR
    clip_bytes          INTEGER,
    clip_error          TEXT,
    retention           TEXT NOT NULL DEFAULT 'new',      -- see RETENTIONS
    retain_until_utc    TEXT,                 -- expiry for keep7
    reviewed_utc        TEXT,                 -- when the user made a retention decision
    UNIQUE (server, review_id)
);
CREATE INDEX IF NOT EXISTS idx_review_retention ON review_items (retention, start_time);

-- Compiled 24h timelapse videos, one rolling 'auto' per camera plus any kept ones.
CREATE TABLE IF NOT EXISTS timelapses (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    server              TEXT NOT NULL,
    camera_id           INTEGER REFERENCES cameras(id) ON DELETE CASCADE,
    camera_name         TEXT NOT NULL,
    created_utc         TEXT NOT NULL,
    covers_start_utc    TEXT,                 -- oldest capture in the video
    covers_end_utc      TEXT,                 -- newest capture in the video
    frame_count         INTEGER,
    path                TEXT,                 -- relative to TIMELAPSE_DIR
    bytes               INTEGER,
    retention           TEXT NOT NULL DEFAULT 'auto',  -- see TL_RETENTIONS
    retain_until_utc    TEXT,
    reviewed_utc        TEXT
);
CREATE INDEX IF NOT EXISTS idx_timelapse_cam ON timelapses (camera_id, created_utc);
"""

DEFAULT_FRONTEND = "main"

ORDER_BY = "ORDER BY sort_order IS NULL, sort_order, id"


def load_config() -> dict[str, str]:
    with open(CONFIG_CSV, newline="") as f:
        return {row["key"].strip(): row["value"].strip() for row in csv.DictReader(f)}


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def _int(value: str) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _seed_cameras(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM cameras LIMIT 1").fetchone() or not CAMERAS_CSV.exists():
        return
    rows = _read_csv(CAMERAS_CSV)
    for i, r in enumerate(rows, start=1):
        conn.execute(
            """INSERT OR IGNORE INTO cameras
               (sort_order, frigate_host, frigate_port, frigate_camera_name, camera_rename, enabled, quality, height)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                i,
                r["frigate_host"],
                _int(r.get("frigate_port")) or 5000,
                r["frigate_camera_name"],
                r.get("camera_rename") or r["frigate_camera_name"],
                0 if r.get("enabled", "true").lower() in ("false", "0", "no") else 1,
                _int(r.get("quality")),
                _int(r.get("height")),
            ),
        )
    log.info("seeded %d cameras from %s", len(rows), CAMERAS_CSV)


def _seed_ha_entities(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM ha_entities LIMIT 1").fetchone() or not HA_ENTITIES_CSV.exists():
        return
    rows = _read_csv(HA_ENTITIES_CSV)
    for i, r in enumerate(rows, start=1):
        conn.execute(
            """INSERT INTO ha_entities (sort_order, label, entity_id, decimals, unit, scale)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                _float(r.get("order")) if r.get("order") else i,
                r.get("label", ""),
                r.get("entity_id", ""),
                _int(r.get("decimals")),
                r.get("unit", ""),
                _float(r.get("scale")),
            ),
        )
    log.info("seeded %d HA entities from %s", len(rows), HA_ENTITIES_CSV)


def _seed_frontends(conn: sqlite3.Connection) -> None:
    """First run: one frontend that shows every camera, so behaviour is unchanged."""
    if conn.execute("SELECT 1 FROM frontends LIMIT 1").fetchone():
        return
    frontend_id = conn.execute("INSERT INTO frontends (name) VALUES (?)", (DEFAULT_FRONTEND,)).lastrowid
    conn.execute(
        "INSERT INTO frontend_cameras (frontend_id, camera_id) SELECT ?, id FROM cameras",
        (frontend_id,),
    )
    log.info("created frontend %r with all cameras", DEFAULT_FRONTEND)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after a table's first release; applied to existing databases.
MIGRATIONS = [
    ("frontends", "allow_review", "INTEGER NOT NULL DEFAULT 1"),
    ("cameras", "download_clips", "INTEGER NOT NULL DEFAULT 1"),
    ("cameras", "make_timelapse", "INTEGER NOT NULL DEFAULT 1"),
    ("frontends", "allow_timelapse", "INTEGER NOT NULL DEFAULT 1"),
    ("fetches", "latest_image", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            log.info("migrated %s: added column %s", table, column)


def init_db() -> None:
    """Create tables and, if the config tables are empty, seed them from the CSVs."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        # Both services call this at startup; the write lock makes seeding happen once
        conn.execute("BEGIN IMMEDIATE")
        _seed_cameras(conn)
        _seed_ha_entities(conn)
        _seed_frontends(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()


def get_cameras(conn: sqlite3.Connection, *, enabled_only: bool = True) -> list[dict]:
    where = "WHERE enabled = 1" if enabled_only else ""
    return [dict(r) for r in conn.execute(f"SELECT * FROM cameras {where} {ORDER_BY}")]


def get_camera(conn: sqlite3.Connection, camera_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM cameras WHERE id = ?", (camera_id,)).fetchone()
    return dict(row) if row else None


def get_frontend(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM frontends WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def get_frontend_cameras(conn: sqlite3.Connection, frontend_id: int) -> list[dict]:
    """Enabled cameras mapped to a frontend, in display order."""
    return [dict(r) for r in conn.execute(
        """SELECT c.* FROM cameras c
           JOIN frontend_cameras fc ON fc.camera_id = c.id
           WHERE fc.frontend_id = ? AND c.enabled = 1
           ORDER BY c.sort_order IS NULL, c.sort_order, c.id""",
        (frontend_id,),
    )]


def get_frontend_camera(conn: sqlite3.Connection, frontend_id: int, camera_id: int) -> Optional[dict]:
    """A camera only if it is enabled and mapped to the frontend."""
    row = conn.execute(
        """SELECT c.* FROM cameras c
           JOIN frontend_cameras fc ON fc.camera_id = c.id
           WHERE fc.frontend_id = ? AND c.id = ? AND c.enabled = 1""",
        (frontend_id, camera_id),
    ).fetchone()
    return dict(row) if row else None


def get_ha_entities(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(f"SELECT * FROM ha_entities {ORDER_BY}")]


def get_servers(conn: sqlite3.Connection) -> list[dict]:
    """Distinct Frigate servers across enabled cameras: {server, port}."""
    rows = conn.execute(
        """SELECT frigate_host AS server, MIN(frigate_port) AS port
           FROM cameras WHERE enabled = 1 GROUP BY frigate_host"""
    ).fetchall()
    return [dict(r) for r in rows]


def frigate_base(server: str, port: int) -> str:
    return f"http://{server}:{port}"


def clip_path(server: str, review_id: str) -> Path:
    key = f"{server}__{review_id}"
    return CLIPS_DIR / (re.sub(r"[^A-Za-z0-9._-]", "_", key) + ".mp4")


def camera_id_for(conn: sqlite3.Connection, server: str, camera_name: str) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM cameras WHERE frigate_host = ? AND frigate_camera_name = ?",
        (server, camera_name),
    ).fetchone()
    return row["id"] if row else None


def get_review_item(conn: sqlite3.Connection, item_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM review_items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def get_frontend_review_item(conn: sqlite3.Connection, frontend_id: int, item_id: int) -> Optional[dict]:
    """A review item only if its camera is enabled and mapped to the frontend."""
    row = conn.execute(
        """SELECT r.* FROM review_items r
           JOIN frontend_cameras fc ON fc.camera_id = r.camera_id
           JOIN cameras c ON c.id = r.camera_id
           WHERE fc.frontend_id = ? AND r.id = ? AND c.enabled = 1""",
        (frontend_id, item_id),
    ).fetchone()
    return dict(row) if row else None


def get_frontend_reviews(conn: sqlite3.Connection, frontend_id: int, retentions: tuple[str, ...]) -> list[dict]:
    """Review items with a downloaded clip, for cameras mapped to the frontend,
    filtered to the given retention states, newest first."""
    placeholders = ",".join("?" * len(retentions))
    rows = conn.execute(
        f"""SELECT r.*, c.camera_rename FROM review_items r
            JOIN frontend_cameras fc ON fc.camera_id = r.camera_id
            JOIN cameras c ON c.id = r.camera_id
            WHERE fc.frontend_id = ? AND c.enabled = 1
              AND r.clip_status = 'downloaded'
              AND r.retention IN ({placeholders})
            ORDER BY r.start_time DESC""",
        (frontend_id, *retentions),
    ).fetchall()
    return [dict(r) for r in rows]


def frigate_url(camera: dict, *, full: bool = False) -> str:
    host = camera["frigate_host"]
    port = camera["frigate_port"]
    name = camera["frigate_camera_name"]
    url = f"http://{host}:{port}/api/{name}/latest.jpg"
    if not full:
        params = []
        if camera.get("quality"):
            params.append(f"quality={camera['quality']}")
        if camera.get("height"):
            params.append(f"height={camera['height']}")  # Frigate 0.17 param is 'height', not 'h'
        if params:
            url += "?" + "&".join(params)
    return url


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def capture_dir(camera: dict) -> Path:
    """Per-camera directory holding retained timestamped stills."""
    return CAPTURES_DIR / str(camera["id"])


def capture_name(camera: dict, dt) -> str:
    """Filename <instance>_<camera>_<yyyymmdd>_<hhmmss>.jpg (datetime is UTC)."""
    return f"{_safe(camera['frigate_host'])}_{_safe(camera['frigate_camera_name'])}_{dt:%Y%m%d_%H%M%S}.jpg"


def capture_rel(camera: dict, filename: str) -> str:
    """Path stored in fetches.latest_image, relative to CAPTURES_DIR."""
    return f"{camera['id']}/{filename}"


def capture_abspath(rel: str) -> Path:
    return CAPTURES_DIR / rel


def timelapse_dir(camera_id: int) -> Path:
    return TIMELAPSE_DIR / str(camera_id)


def get_fetch(conn: sqlite3.Connection, camera: dict) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM fetches WHERE frigate_host = ? AND frigate_camera_name = ?",
        (camera["frigate_host"], camera["frigate_camera_name"]),
    ).fetchone()


def latest_image_abspath(conn: sqlite3.Connection, camera: dict) -> Optional[Path]:
    """Absolute path of the newest capture the DB points to, or None."""
    row = get_fetch(conn, camera)
    if row and row["latest_image"]:
        return capture_abspath(row["latest_image"])
    return None


def timelapse_cameras(conn: sqlite3.Connection) -> list[dict]:
    """Enabled cameras that should retain captures and get a timelapse."""
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM cameras WHERE enabled = 1 AND make_timelapse = 1 {ORDER_BY}"
    )]


def get_timelapse(conn: sqlite3.Connection, timelapse_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM timelapses WHERE id = ?", (timelapse_id,)).fetchone()
    return dict(row) if row else None


def get_frontend_timelapse(conn: sqlite3.Connection, frontend_id: int, timelapse_id: int) -> Optional[dict]:
    row = conn.execute(
        """SELECT t.* FROM timelapses t
           JOIN frontend_cameras fc ON fc.camera_id = t.camera_id
           JOIN cameras c ON c.id = t.camera_id
           WHERE fc.frontend_id = ? AND t.id = ? AND c.enabled = 1""",
        (frontend_id, timelapse_id),
    ).fetchone()
    return dict(row) if row else None


def get_frontend_timelapses(conn: sqlite3.Connection, frontend_id: int) -> list[dict]:
    """Timelapses (with a file) for cameras mapped to the frontend, newest first."""
    rows = conn.execute(
        """SELECT t.*, c.camera_rename FROM timelapses t
           JOIN frontend_cameras fc ON fc.camera_id = t.camera_id
           JOIN cameras c ON c.id = t.camera_id
           WHERE fc.frontend_id = ? AND c.enabled = 1 AND t.path IS NOT NULL
           ORDER BY t.created_utc DESC""",
        (frontend_id,),
    ).fetchall()
    return [dict(r) for r in rows]
