# Camera Monitor — Specification

## Purpose

A lightweight web app that rotates through a list of RTSP cameras, capturing and displaying a single still frame per camera per rotation. The goal is to monitor remote cameras over a low-bandwidth Starlink connection (~20–30 Mbps upload) without the overhead of live streaming relatively static scenes.

## Behaviour

- Cycles through all configured cameras in order, looping continuously.
- On each rotation, captures **one JPEG frame** from the camera's RTSP stream using ffmpeg; overwrites any previous frame for that camera.
- Displays the captured frame full-screen for the configured rotation interval, then advances to the next camera.
- While the current camera is displayed, the server captures the **next** camera's frame in the background (pre-fetch), so it is ready when the rotation advances.

## Configuration

All configuration is via CSV files mounted into the container.

**config.csv** — global app settings:

```
key,value
rotation_interval,10
```

- `rotation_interval` — seconds each camera is displayed before advancing (default: `10`)

**cameras.csv** — one row per camera, header row required:

```
name,url,username,password
```

- `name` — human-readable label displayed in the UI overlay
- `url` — base RTSP URL (e.g. `rtsp://192.168.1.100:554/stream1`), without credentials
- `username` / `password` — RTSP credentials; embedded into the URL at capture time

## Stack

- **Backend**: Python, FastAPI, uvicorn
- **Frame capture**: ffmpeg (`-vframes 1`, `-rtsp_transport tcp`)
- **Frontend**: single static HTML/JS page served by FastAPI
- **Deployment**: Docker (local to the viewing location, no round-trip over Starlink)

## UI

- Full-screen single-camera view (`object-fit: contain`, black background)
- Camera name overlaid in the top-left corner
- If a frame cannot be captured, an error message is shown in place of the image
- No authentication required

## Error Handling

- Per-camera capture errors (timeout, connection refused, bad credentials) are stored and surfaced as an error message in the UI for that camera's slot in the rotation.
- A capture timeout of 8 seconds is enforced per frame.
- The server continues cycling; a failed camera does not halt the rotation.
