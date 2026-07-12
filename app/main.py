from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .ffmpeg import CaptureProcess, ReplaySaveResult, cleanup_old_chunks, discover_ffmpeg_path, ffmpeg_discovery_error, list_dshow_devices, recent_chunks, save_replay


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class PollingAccessFilter(logging.Filter):
    """Hide successful background polling while retaining errors and mutations."""

    QUIET_PATHS = {"/api/status", "/api/replays"}

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        method, raw_path, status_code = args[1], str(args[2]), args[4]
        path = raw_path.split("?", 1)[0]
        return not (method == "GET" and path in self.QUIET_PATHS and int(status_code) < 400)


logging.getLogger("uvicorn.access").addFilter(PollingAccessFilter())
APP_VERSION = "mjpeg-live-v6"

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
capture = CaptureProcess(settings)
replay_lock = asyncio.Lock()
replay_task: asyncio.Task[ReplaySaveResult] | None = None


async def cleanup_loop() -> None:
    while True:
        await cleanup_old_chunks(settings)
        await asyncio.sleep(max(settings.chunk_seconds, 1))


async def capture_loop() -> None:
    while True:
        if not capture.is_running():
            try:
                capture.start()
            except Exception:
                logging.exception("Failed to start capture")
        elif capture.live_frame_age_seconds() is not None and capture.live_frame_age_seconds() > 15:
            logging.warning("Live frames stalled for %s seconds; restarting capture", capture.live_frame_age_seconds())
            capture.stop()
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    cleanup_task = asyncio.create_task(cleanup_loop())
    capture_task = asyncio.create_task(capture_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        capture_task.cancel()
        capture.stop()


app = FastAPI(title="Instant Replay Camera", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "replay_minutes": settings.replay_minutes,
            "chunk_seconds": settings.chunk_seconds,
            "camera_rotation_degrees": settings.camera_rotation_degrees,
            "app_version": APP_VERSION,
        },
    )


@app.get("/highlights", response_class=HTMLResponse)
async def highlights(request: Request):
    return templates.TemplateResponse(
        "highlights.html",
        {
            "request": request,
            "app_version": APP_VERSION,
        },
    )

@app.get("/api/status")
async def status():
    chunks = recent_chunks(settings, settings.max_buffer_seconds)
    capture_status = capture.status()
    stream_warning = None
    frame_age = capture_status["live_frame_age_seconds"]
    live_ready = capture_status["running"] and capture.latest_frame is not None and (frame_age is None or frame_age < 5)
    if not capture_status["running"]:
        stream_warning = capture_status["last_error"] or "Camera capture is not running"
    elif not live_ready:
        stream_warning = "Camera capture is running, waiting for fresh live frames"
    return {
        "app_version": APP_VERSION,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "capture_running": capture_status["running"],
        "capture": capture_status,
        "live_mode": "mjpeg",
        "live_url": "/live.mjpg",
        "live_ready": live_ready,
        "replay_minutes": settings.replay_minutes,
        "max_buffer_minutes": settings.max_buffer_minutes,
        "chunk_seconds": settings.chunk_seconds,
        "camera_rotation_degrees": settings.camera_rotation_degrees,
        "buffered_chunks": len(chunks),
        "buffered_seconds_estimate": len(chunks) * settings.chunk_seconds,
        "stream_warning": stream_warning,
        "ffmpeg_path": discover_ffmpeg_path() or settings.ffmpeg_path,
        "ffmpeg_error": ffmpeg_discovery_error(),
    }


@app.get("/api/devices")
async def devices():
    if settings.input_mode != "dshow":
        return {
            "video": [],
            "audio": [],
            "error": f"Device detection is only implemented for dshow, current mode is {settings.input_mode}",
            "ffmpeg_path": discover_ffmpeg_path() or settings.ffmpeg_path,
            "ffmpeg_error": ffmpeg_discovery_error(),
        }
    ffmpeg_path = discover_ffmpeg_path() or settings.ffmpeg_path
    inventory = list_dshow_devices(ffmpeg_path)
    return {
        "video": inventory.video,
        "audio": inventory.audio,
        "error": inventory.error,
        "ffmpeg_path": ffmpeg_path,
        "ffmpeg_error": ffmpeg_discovery_error(),
    }


@app.get("/live.mjpg")
async def live_mjpeg():
    async def frames():
        last_count = -1
        capture.live_clients += 1
        try:
            while capture.is_running():
                async with capture.frame_condition:
                    try:
                        await asyncio.wait_for(
                            capture.frame_condition.wait_for(lambda: capture.frame_count != last_count or not capture.is_running()),
                            timeout=10,
                        )
                    except asyncio.TimeoutError:
                        break
                    if capture.latest_frame is None:
                        continue
                    frame = capture.latest_frame
                    last_count = capture.frame_count
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Cache-Control: no-store\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        finally:
            capture.live_clients = max(0, capture.live_clients - 1)

    return StreamingResponse(
        frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/replays")
async def create_replay():
    global replay_task

    async with replay_lock:
        if replay_task is None or replay_task.done():
            replay_task = asyncio.create_task(save_replay(settings))
            deduplicated = False
        else:
            deduplicated = True

    try:
        result = await replay_task
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "file": result.output.name,
        "url": f"/replays/{result.output.name}",
        "deduplicated": deduplicated,
        "backup_file": str(result.backup_path) if result.backup_path else None,
        "backup_error": result.backup_error,
    }


@app.get("/api/replays")
async def list_replays():
    files = sorted(
        settings.replay_dir.glob("replay_*.mp4"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return [
        {
            "file": path.name,
            "url": f"/replays/{path.name}",
            "bytes": path.stat().st_size,
            "modified": path.stat().st_mtime,
        }
        for path in files
    ]


@app.get("/replays/{filename}")
async def download_replay(filename: str):
    path = settings.replay_dir / filename
    if not path.is_file() or path.parent != settings.replay_dir:
        raise HTTPException(status_code=404, detail="Replay not found")
    # Serve inline; the highlights page uses a download attribute when needed.
    return FileResponse(path, media_type="video/mp4")
