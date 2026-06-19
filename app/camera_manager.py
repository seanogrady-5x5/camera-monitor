from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)

CAPTURE_TIMEOUT = 10.0
CYCLE_PAUSE = 2.0


class CameraManager:
    def __init__(self, cameras: list[dict]):
        self.cameras = cameras
        self._frames: dict[int, Optional[bytes]] = {}
        self._errors: dict[int, Optional[str]] = {}
        self._client = httpx.AsyncClient(timeout=CAPTURE_TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    def get_frame(self, index: int) -> tuple[Optional[bytes], Optional[str]]:
        return self._frames.get(index), self._errors.get(index)

    def _frame_url(self, camera: dict) -> str:
        host = camera["frigate_host"]
        port = camera["frigate_port"]
        name = camera["frigate_camera_name"]
        return f"http://{host}:{port}/api/{name}/latest.jpg"

    async def _capture(self, index: int) -> None:
        for attempt in range(1, 4):
            try:
                await self._capture_once(index)
                return
            except Exception as exc:
                log.warning(
                    "camera %d (%s) attempt %d/3: %s",
                    index, self.cameras[index].get("frigate_camera_name", "?"), attempt, exc,
                )
                self._errors[index] = str(exc)
                if attempt < 3:
                    await asyncio.sleep(2)

    async def _capture_once(self, index: int) -> None:
        url = self._frame_url(self.cameras[index])
        response = await self._client.get(url)
        response.raise_for_status()

        data = response.content
        if not data:
            raise RuntimeError("empty response from Frigate")

        self._frames[index] = data
        self._errors[index] = None

    async def run(self) -> None:
        while True:
            for i in range(len(self.cameras)):
                await self._capture(i)
            await asyncio.sleep(CYCLE_PAUSE)
