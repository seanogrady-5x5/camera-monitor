from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager, closing
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field

try:
    from app.common import (
        BASE_DIR, connect, frigate_url, get_camera, get_cameras, get_fetch,
        get_ha_entities, image_path, init_db, load_config,
    )
except ImportError:
    from common import (
        BASE_DIR, connect, frigate_url, get_camera, get_cameras, get_fetch,
        get_ha_entities, image_path, init_db, load_config,
    )

DOWNLOAD_TIMEOUT = 10.0

rotation_interval: int = 10
client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global rotation_interval, client

    rotation_interval = int(load_config().get("rotation_interval", "10"))
    init_db()
    client = httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT)

    yield

    await client.aclose()


app = FastAPI(title="Camera Monitor API", lifespan=lifespan)


def camera_or_404(conn: sqlite3.Connection, camera_id: int) -> dict:
    camera = get_camera(conn, camera_id)
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    return camera


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/admin")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return Response(
        content=(BASE_DIR / "static" / "admin.html").read_bytes(),
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/health")
def health():
    with closing(connect()) as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/config")
def get_config():
    with closing(connect()) as conn:
        cameras = get_cameras(conn)
    return {
        "cameras": [{"id": c["id"], "name": c["camera_rename"]} for c in cameras],
        "rotation_interval": rotation_interval,
    }


@app.get("/api/frame/{camera_id}")
def get_frame(camera_id: int):
    with closing(connect()) as conn:
        camera = camera_or_404(conn, camera_id)

    try:
        frame = image_path(camera).read_bytes()
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="Frame not yet available")

    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/status/{camera_id}")
def get_status(camera_id: int):
    with closing(connect()) as conn:
        row = get_fetch(conn, camera_or_404(conn, camera_id))

    keys = ("last_attempt_utc", "last_fetched_utc", "payload_bytes", "last_error")
    return {k: (row[k] if row else None) for k in keys}


UNIT_CONVERSIONS = {
    ("W", "kW"): lambda v: v / 1000,
    ("kW", "W"): lambda v: v * 1000,
    ("Wh", "kWh"): lambda v: v / 1000,
    ("kWh", "Wh"): lambda v: v * 1000,
    ("°F", "°C"): lambda v: (v - 32) * 5 / 9,
    ("°C", "°F"): lambda v: v * 9 / 5 + 32,
}


def to_display_unit(
    state: str | None, ha_unit: str | None, unit: str, scale: float | None = None,
) -> tuple[str | None, str | None]:
    """Convert a HA state into the configured unit where possible.

    An explicit scale multiplies the value and labels it with the configured
    unit as-is, bypassing automatic conversion.
    """
    if scale is not None:
        try:
            return str(float(state) * scale), unit or ha_unit
        except (TypeError, ValueError):
            return state, ha_unit  # non-numeric state
    if not unit or unit == ha_unit:
        return state, ha_unit or unit or None
    if not ha_unit:
        return state, unit
    convert = UNIT_CONVERSIONS.get((ha_unit, unit))
    if convert is None:
        return state, ha_unit  # unknown conversion: keep HA's unit rather than mislabel
    try:
        return str(convert(float(state))), unit
    except (TypeError, ValueError):
        return state, ha_unit  # non-numeric, e.g. "unavailable"


@app.get("/api/stats")
def get_stats():
    with closing(connect()) as conn:
        entities = get_ha_entities(conn)
        rows = {r["entity_id"]: r for r in conn.execute("SELECT * FROM ha_states")}

    stats = []
    for e in entities:
        row = rows.get(e["entity_id"])
        state, unit = to_display_unit(
            row["state"] if row else None,
            row["unit"] if row else None,
            e["unit"],
            e["scale"],
        )
        stats.append({
            "label": e["label"],
            "entity_id": e["entity_id"] or None,
            "decimals": e["decimals"],
            "state": state,
            "unit": unit,
            "last_fetched_utc": row["last_fetched_utc"] if row else None,
            "last_error": row["last_error"] if row else None,
        })
    return {"stats": stats}


@app.get("/api/frame/{camera_id}/original")
async def get_original_frame(camera_id: int):
    with closing(connect()) as conn:
        camera = camera_or_404(conn, camera_id)

    try:
        response = await client.get(frigate_url(camera, full=True))
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return Response(
        content=response.content,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{camera["frigate_camera_name"]}.jpg"',
        },
    )


# ---- Admin: camera and HA entity configuration ----


class CameraIn(BaseModel):
    sort_order: Optional[float] = None
    frigate_host: str = Field(min_length=1)
    frigate_port: int = Field(5000, ge=1, le=65535)
    frigate_camera_name: str = Field(min_length=1)
    camera_rename: str = Field(min_length=1)
    enabled: bool = True
    quality: Optional[int] = Field(None, ge=1, le=100)
    height: Optional[int] = Field(None, ge=1)


class HaEntityIn(BaseModel):
    sort_order: Optional[float] = None
    label: str = Field(min_length=1)
    entity_id: str = ""
    decimals: Optional[int] = Field(None, ge=0, le=6)
    unit: str = ""
    scale: Optional[float] = None


CAMERA_FIELDS = list(CameraIn.model_fields)
HA_ENTITY_FIELDS = list(HaEntityIn.model_fields)


def _write(conn: sqlite3.Connection, sql: str, params: tuple) -> sqlite3.Cursor:
    try:
        with conn:
            return conn.execute(sql, params)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail=f"Conflicts with an existing row: {exc}")


def _insert(conn: sqlite3.Connection, table: str, fields: list[str], body: BaseModel) -> int:
    values = [getattr(body, f) for f in fields]
    sql = f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})"
    return _write(conn, sql, tuple(values)).lastrowid


def _update(conn: sqlite3.Connection, table: str, fields: list[str], row_id: int, body: BaseModel) -> None:
    values = [getattr(body, f) for f in fields]
    sql = f"UPDATE {table} SET {', '.join(f'{f} = ?' for f in fields)} WHERE id = ?"
    if _write(conn, sql, (*values, row_id)).rowcount == 0:
        raise HTTPException(status_code=404, detail="Not found")


@app.get("/api/admin/cameras")
def admin_list_cameras():
    with closing(connect()) as conn:
        cameras = get_cameras(conn, enabled_only=False)
        for c in cameras:
            row = get_fetch(conn, c)
            c["enabled"] = bool(c["enabled"])
            c["last_fetched_utc"] = row["last_fetched_utc"] if row else None
            c["last_error"] = row["last_error"] if row else None
    return {"cameras": cameras}


@app.post("/api/admin/cameras", status_code=201)
def admin_create_camera(body: CameraIn):
    with closing(connect()) as conn:
        return {"id": _insert(conn, "cameras", CAMERA_FIELDS, body)}


@app.put("/api/admin/cameras/{camera_id}")
def admin_update_camera(camera_id: int, body: CameraIn):
    with closing(connect()) as conn:
        _update(conn, "cameras", CAMERA_FIELDS, camera_id, body)
    return {"id": camera_id}


@app.delete("/api/admin/cameras/{camera_id}", status_code=204)
def admin_delete_camera(camera_id: int):
    with closing(connect()) as conn:
        camera = camera_or_404(conn, camera_id)
        with conn:
            conn.execute("DELETE FROM cameras WHERE id = ?", (camera_id,))
            conn.execute(
                "DELETE FROM fetches WHERE frigate_host = ? AND frigate_camera_name = ?",
                (camera["frigate_host"], camera["frigate_camera_name"]),
            )
    image_path(camera).unlink(missing_ok=True)
    return Response(status_code=204)


@app.get("/api/admin/ha-entities")
def admin_list_ha_entities():
    with closing(connect()) as conn:
        entities = get_ha_entities(conn)
        states = {r["entity_id"]: r for r in conn.execute("SELECT * FROM ha_states")}
    for e in entities:
        row = states.get(e["entity_id"])
        e["state"] = row["state"] if row else None
        e["ha_unit"] = row["unit"] if row else None
        e["last_error"] = row["last_error"] if row else None
    return {"ha_entities": entities}


@app.post("/api/admin/ha-entities", status_code=201)
def admin_create_ha_entity(body: HaEntityIn):
    with closing(connect()) as conn:
        return {"id": _insert(conn, "ha_entities", HA_ENTITY_FIELDS, body)}


@app.put("/api/admin/ha-entities/{entity_row_id}")
def admin_update_ha_entity(entity_row_id: int, body: HaEntityIn):
    with closing(connect()) as conn:
        _update(conn, "ha_entities", HA_ENTITY_FIELDS, entity_row_id, body)
    return {"id": entity_row_id}


@app.delete("/api/admin/ha-entities/{entity_row_id}", status_code=204)
def admin_delete_ha_entity(entity_row_id: int):
    with closing(connect()) as conn:
        with conn:
            cur = conn.execute("DELETE FROM ha_entities WHERE id = ?", (entity_row_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(status_code=204)
