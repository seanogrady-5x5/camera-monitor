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
DB_PATH = DATA_DIR / "camera-monitor.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fetches (
    frigate_host        TEXT NOT NULL,
    frigate_camera_name TEXT NOT NULL,
    last_attempt_utc    TEXT NOT NULL,
    last_fetched_utc    TEXT,
    payload_bytes       INTEGER,
    last_error          TEXT,
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
    show_errors         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS frontend_cameras (
    frontend_id         INTEGER NOT NULL REFERENCES frontends(id) ON DELETE CASCADE,
    camera_id           INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    PRIMARY KEY (frontend_id, camera_id)
);
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


def init_db() -> None:
    """Create tables and, if the config tables are empty, seed them from the CSVs."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
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
            params.append(f"h={camera['height']}")
        if params:
            url += "?" + "&".join(params)
    return url


def image_path(camera: dict) -> Path:
    key = f"{camera['frigate_host']}__{camera['frigate_camera_name']}"
    return IMAGES_DIR / (re.sub(r"[^A-Za-z0-9._-]", "_", key) + ".jpg")


def get_fetch(conn: sqlite3.Connection, camera: dict) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM fetches WHERE frigate_host = ? AND frigate_camera_name = ?",
        (camera["frigate_host"], camera["frigate_camera_name"]),
    ).fetchone()
