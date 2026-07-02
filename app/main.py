from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .ffmpeg import CaptureProcess, cleanup_old_chunks, discover_ffmpeg_path, list_dshow_devices, recent_chunks, save_replay


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
capture = CaptureProcess(settings)


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
        },
    )


@app.get("/api/status")
async def status():
    chunks = recent_chunks(settings, settings.max_buffer_seconds)
    capture_status = capture.status()
    hls_ready = (settings.hls_dir / "live.m3u8").is_file()
    return {
        "capture_running": capture_status["running"],
        "capture": capture_status,
        "replay_minutes": settings.replay_minutes,
        "max_buffer_minutes": settings.max_buffer_minutes,
        "chunk_seconds": settings.chunk_seconds,
        "buffered_chunks": len(chunks),
        "buffered_seconds_estimate": len(chunks) * settings.chunk_seconds,
        "live_hls": "/static/hls/live.m3u8",
        "live_hls_ready": hls_ready,
        "ffmpeg_path": discover_ffmpeg_path() or settings.ffmpeg_path,
    }


@app.get("/api/devices")
async def devices():
    if settings.input_mode != "dshow":
        return {"video": [], "audio": [], "error": f"Device detection is only implemented for dshow, current mode is {settings.input_mode}"}
    inventory = list_dshow_devices(discover_ffmpeg_path() or settings.ffmpeg_path)
    return {"video": inventory.video, "audio": inventory.audio, "error": inventory.error}


@app.post("/api/replays")
async def create_replay():
    try:
        output = await save_replay(settings)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"file": output.name, "url": f"/replays/{output.name}"}


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
