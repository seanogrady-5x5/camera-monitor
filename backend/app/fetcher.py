from __future__ import annotations

import logging
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

import httpx

try:
    from app.common import (
        IMAGES_DIR, connect, frigate_url, get_cameras, get_ha_entities,
        image_path, init_db, load_config,
    )
except ImportError:
    from common import (
        IMAGES_DIR, connect, frigate_url, get_cameras, get_ha_entities,
        image_path, init_db, load_config,
    )

log = logging.getLogger("fetcher")

FETCH_TIMEOUT = 10.0

RECORD_SUCCESS = """
INSERT INTO fetches (frigate_host, frigate_camera_name, last_attempt_utc, last_fetched_utc, payload_bytes, last_error)
VALUES (?, ?, ?, ?, ?, NULL)
ON CONFLICT (frigate_host, frigate_camera_name) DO UPDATE SET
    last_attempt_utc = excluded.last_attempt_utc,
    last_fetched_utc = excluded.last_fetched_utc,
    payload_bytes    = excluded.payload_bytes,
    last_error       = NULL
"""

RECORD_FAILURE = """
INSERT INTO fetches (frigate_host, frigate_camera_name, last_attempt_utc, last_error)
VALUES (?, ?, ?, ?)
ON CONFLICT (frigate_host, frigate_camera_name) DO UPDATE SET
    last_attempt_utc = excluded.last_attempt_utc,
    last_error       = excluded.last_error
"""

RECORD_HA_SUCCESS = """
INSERT INTO ha_states (entity_id, last_attempt_utc, last_fetched_utc, state, unit, ha_last_updated, last_error)
VALUES (?, ?, ?, ?, ?, ?, NULL)
ON CONFLICT (entity_id) DO UPDATE SET
    last_attempt_utc = excluded.last_attempt_utc,
    last_fetched_utc = excluded.last_fetched_utc,
    state            = excluded.state,
    unit             = excluded.unit,
    ha_last_updated  = excluded.ha_last_updated,
    last_error       = NULL
"""

RECORD_HA_FAILURE = """
INSERT INTO ha_states (entity_id, last_attempt_utc, last_error)
VALUES (?, ?, ?)
ON CONFLICT (entity_id) DO UPDATE SET
    last_attempt_utc = excluded.last_attempt_utc,
    last_error       = excluded.last_error
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch_one(client: httpx.Client, camera: dict) -> tuple[dict, str, Optional[str], Optional[int], Optional[str]]:
    attempted = utc_now()
    try:
        response = client.get(frigate_url(camera))
        response.raise_for_status()

        data = response.content
        if not data:
            raise RuntimeError("empty response from Frigate")

        # Write then rename so readers never see a partial file
        path = image_path(camera)
        tmp = path.with_suffix(".jpg.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

        return camera, attempted, utc_now(), len(data), None
    except Exception as exc:
        log.warning("%s/%s: %s", camera["frigate_host"], camera["frigate_camera_name"], exc)
        return camera, attempted, None, None, str(exc)


def fetch_ha_state(client: httpx.Client, ha_url: str, entity_id: str) -> tuple:
    attempted = utc_now()
    try:
        response = client.get(f"{ha_url}/api/states/{entity_id}")
        response.raise_for_status()
        body = response.json()
        unit = body.get("attributes", {}).get("unit_of_measurement")
        return entity_id, attempted, utc_now(), body.get("state"), unit, body.get("last_updated"), None
    except Exception as exc:
        log.warning("home assistant %s: %s", entity_id, exc)
        return entity_id, attempted, None, None, None, None, str(exc)


def record(conn: sqlite3.Connection, results: list, ha_results: list) -> None:
    for camera, attempted, fetched, size, error in results:
        key = (camera["frigate_host"], camera["frigate_camera_name"])
        if error is None:
            conn.execute(RECORD_SUCCESS, (*key, attempted, fetched, size))
        else:
            conn.execute(RECORD_FAILURE, (*key, attempted, error))

    for entity_id, attempted, fetched, state, unit, ha_updated, error in ha_results:
        if error is None:
            conn.execute(RECORD_HA_SUCCESS, (entity_id, attempted, fetched, state, unit, ha_updated))
        else:
            conn.execute(RECORD_HA_FAILURE, (entity_id, attempted, error))
    conn.commit()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = load_config()
    interval = int(cfg.get("fetch_interval", "15"))
    workers = int(cfg.get("fetch_workers", "8"))

    ha_url = os.environ.get("HA_URL", "").strip().rstrip("/")
    ha_token = os.environ.get("HA_TOKEN", "").strip()
    ha_enabled = bool(ha_url and ha_token)
    if not ha_enabled:
        log.info("HA_URL/HA_TOKEN not set; skipping home assistant stats")

    init_db()
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    log.info("fetching every %ds with %d workers", interval, workers)

    ha_headers = {"Authorization": f"Bearer {ha_token}"}
    with (
        httpx.Client(timeout=FETCH_TIMEOUT) as client,
        httpx.Client(timeout=FETCH_TIMEOUT, headers=ha_headers) as ha_client,
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        while True:
            started = time.monotonic()

            # Re-read config each cycle so admin-page edits apply without a restart
            cameras = get_cameras(conn)
            entity_ids = []
            if ha_enabled:
                entity_ids = list(dict.fromkeys(e["entity_id"] for e in get_ha_entities(conn) if e["entity_id"]))

            camera_futures = [pool.submit(fetch_one, client, c) for c in cameras]
            ha_futures = [pool.submit(fetch_ha_state, ha_client, ha_url, e) for e in entity_ids]
            results = [f.result() for f in camera_futures]
            ha_results = [f.result() for f in ha_futures]
            record(conn, results, ha_results)

            failed = sum(1 for r in results if r[4] is not None)
            ha_failed = sum(1 for r in ha_results if r[6] is not None)
            elapsed = time.monotonic() - started
            log.info(
                "cycle done in %.1fs: cameras %d ok, %d failed; HA %d ok, %d failed",
                elapsed, len(results) - failed, failed, len(ha_results) - ha_failed, ha_failed,
            )
            time.sleep(max(0.0, interval - elapsed))


if __name__ == "__main__":
    main()
