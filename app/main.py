from __future__ import annotations

import asyncio
import csv
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from camera_manager import CameraManager

BASE_DIR = Path(__file__).parent
CAMERAS_CSV = BASE_DIR / "cameras.csv"
CONFIG_CSV = BASE_DIR / "config.csv"
STATIC_DIR = BASE_DIR / "static"

cameras: list[dict] = []
rotation_interval: int = 10
manager: CameraManager | None = None


def load_config() -> int:
    with open(CONFIG_CSV, newline="") as f:
        cfg = {row["key"].strip(): row["value"].strip() for row in csv.DictReader(f)}
    return int(cfg.get("rotation_interval", "10"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global cameras, rotation_interval, manager

    rotation_interval = load_config()

    with open(CAMERAS_CSV, newline="") as f:
        reader = csv.DictReader(f)
        all_rows = [{k: v.strip() for k, v in row.items()} for row in reader]

    cameras = [
        c for c in all_rows
        if c.get("enabled", "true").lower() not in ("false", "0", "no")
    ]

    if not cameras:
        raise RuntimeError(f"No enabled cameras found in {CAMERAS_CSV}")

    manager = CameraManager(cameras)
    task = asyncio.create_task(manager.run())

    yield

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def get_config():
    return {
        "cameras": [{"index": i, "name": c["name"]} for i, c in enumerate(cameras)],
        "rotation_interval": rotation_interval,
    }


@app.get("/api/frame/{index}")
async def get_frame(index: int):
    if manager is None or not (0 <= index < len(cameras)):
        raise HTTPException(status_code=404, detail="Camera not found")

    frame, error = manager.get_frame(index)

    if error:
        raise HTTPException(status_code=503, detail=error)
    if frame is None:
        raise HTTPException(status_code=503, detail="Frame not yet available")

    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )
