from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .ffmpeg import CaptureProcess, cleanup_old_chunks, discover_ffmpeg_path, ffmpeg_discovery_error, list_dshow_devices, recent_chunks, save_replay


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

APP_VERSION = "offline-hls-v4"
NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
capture = CaptureProcess(settings)
replay_lock = asyncio.Lock()
replay_task: asyncio.Task[Path] | None = None


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
            "app_version": APP_VERSION,
        },
    )


def hls_diagnostics() -> dict[str, object]:
    files = [path for path in settings.hls_dir.glob("*") if path.is_file()]
    playlist = settings.hls_dir / "live.m3u8"
    latest = max(files, key=lambda path: path.stat().st_mtime, default=None)
    latest_timestamp = latest.stat().st_mtime if latest else None
    latest_age = (datetime.now().timestamp() - latest_timestamp) if latest_timestamp else None
    return {
        "ready": playlist.is_file(),
        "file_count": len(files),
        "latest_file": latest.name if latest else None,
        "latest_file_modified": latest_timestamp,
        "latest_file_age_seconds": round(latest_age, 1) if latest_age is not None else None,
    }


@app.get("/api/status")
async def status():
    chunks = recent_chunks(settings, settings.max_buffer_seconds)
    capture_status = capture.status()
    hls = hls_diagnostics()
    stream_warning = None
    if capture_status["running"] and not hls["ready"]:
        stream_warning = "Capture is running, but the live HLS playlist has not been created yet"
    return {
        "app_version": APP_VERSION,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "capture_running": capture_status["running"],
        "capture": capture_status,
        "replay_minutes": settings.replay_minutes,
        "max_buffer_minutes": settings.max_buffer_minutes,
        "chunk_seconds": settings.chunk_seconds,
        "buffered_chunks": len(chunks),
        "buffered_seconds_estimate": len(chunks) * settings.chunk_seconds,
        "live_hls": "/live/live.m3u8",
        "live_hls_ready": hls["ready"],
        "live_hls_file_count": hls["file_count"],
        "live_hls_latest_file": hls["latest_file"],
        "live_hls_latest_file_modified": hls["latest_file_modified"],
        "live_hls_latest_file_age_seconds": hls["latest_file_age_seconds"],
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


@app.get("/live/live.m3u8")
async def live_playlist():
    path = settings.hls_dir / "live.m3u8"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Live stream is not ready")
    content = path.read_text(encoding="utf-8", errors="replace")
    return Response(content, media_type="application/vnd.apple.mpegurl", headers=NO_STORE_HEADERS)


@app.get("/live/{segment_name}")
async def live_segment(segment_name: str):
    if "/" in segment_name or "\\" in segment_name or segment_name.startswith("."):
        raise HTTPException(status_code=404, detail="Segment not found")
    path = settings.hls_dir / segment_name
    if not path.is_file() or path.parent != settings.hls_dir:
        raise HTTPException(status_code=404, detail="Segment not found")
    media_type = "video/mp2t" if path.suffix.lower() == ".ts" else "application/octet-stream"
    return FileResponse(path, media_type=media_type, headers=NO_STORE_HEADERS)


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
        output = await replay_task
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"file": output.name, "url": f"/replays/{output.name}", "deduplicated": deduplicated}


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
    return FileResponse(path, media_type="video/mp4", filename=filename)
