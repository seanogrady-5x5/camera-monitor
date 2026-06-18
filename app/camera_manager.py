from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Optional
from urllib.parse import urlparse, urlunparse

log = logging.getLogger(__name__)

CAPTURE_TIMEOUT = 15.0
CYCLE_PAUSE = 2.0


class CameraManager:
    def __init__(self, cameras: list[dict]):
        self.cameras = cameras
        self._frames: dict[int, Optional[bytes]] = {}
        self._errors: dict[int, Optional[str]] = {}

    def get_frame(self, index: int) -> tuple[Optional[bytes], Optional[str]]:
        return self._frames.get(index), self._errors.get(index)

    def _build_url(self, camera: dict) -> str:
        url = camera["url"]
        username = camera.get("username", "")
        password = camera.get("password", "")
        if not username:
            return url
        parsed = urlparse(url)
        netloc = f"{username}:{password}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse(
            (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
        )

    async def _capture(self, index: int) -> None:
        for attempt in range(1, 4):
            try:
                await self._capture_once(index)
                return
            except Exception as exc:
                log.warning(
                    "camera %d (%s) attempt %d/3: %s",
                    index, self.cameras[index].get("name", "?"), attempt, exc,
                )
                self._errors[index] = str(exc)
                if attempt < 3:
                    await asyncio.sleep(2)

    async def _capture_once(self, index: int) -> None:
        rtsp_url = self._build_url(self.cameras[index])

        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp_path = tmp.name
        tmp.close()

        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-rtsp_transport", "tcp",
                "-i", rtsp_url,
                "-vframes", "1",
                "-q:v", "2",
                tmp_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=CAPTURE_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise RuntimeError("capture timed out after 15s")

            if proc.returncode != 0:
                stderr_text = stderr_bytes.decode(errors="replace").strip()
                last_line = stderr_text.splitlines()[-1] if stderr_text else "no output"
                raise RuntimeError(f"ffmpeg error: {last_line}")

            with open(tmp_path, "rb") as f:
                data = f.read()

            if not data:
                raise RuntimeError("ffmpeg produced an empty file")

            self._frames[index] = data
            self._errors[index] = None

        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    async def run(self) -> None:
        while True:
            for i in range(len(self.cameras)):
                await self._capture(i)
            await asyncio.sleep(CYCLE_PAUSE)
