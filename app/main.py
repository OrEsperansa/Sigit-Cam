from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import (
    COOKIE_NAME,
    LoginAttemptLimiter,
    PasswordAuthMiddleware,
    csrf_token,
    is_authenticated,
    safe_next_path,
    session_token,
)
from .config import settings
from .ffmpeg import CaptureProcess, ReplaySaveResult, discover_ffmpeg_path, ffmpeg_discovery_error, list_dshow_devices, save_replay
from .replays import ReplayCatalog
from .settings_manager import CAPTURE_RESET_FIELDS, SettingsManager


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger("sigit.app")


class PollingAccessFilter(logging.Filter):
    QUIET_PATHS = {"/api/status", "/api/replays", "/api/audio-level", "/api/storage"}

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        method, raw_path, status_code = args[1], str(args[2]), args[4]
        path = raw_path.split("?", 1)[0]
        return not (method == "GET" and path in self.QUIET_PATHS and int(status_code) < 400)


logging.getLogger("uvicorn.access").addFilter(PollingAccessFilter())
APP_VERSION = "reliability-ux-v2"

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
settings_manager = SettingsManager(settings)
capture = CaptureProcess(settings_manager.current)
catalog = ReplayCatalog(settings_manager.current)
capture_control_lock = asyncio.Lock()
replay_lock = asyncio.Lock()
replay_task: asyncio.Task[ReplaySaveResult] | None = None
login_limiter = LoginAttemptLimiter()


def _is_expected_client_disconnect(context: dict[str, object]) -> bool:
    exception = context.get("exception")
    return isinstance(exception, ConnectionResetError) and getattr(exception, "winerror", None) == 10054


def _template_context(request: Request, **values: object) -> dict[str, object]:
    return {
        "request": request,
        "app_version": APP_VERSION,
        "csrf_token": csrf_token(settings),
        **values,
    }


async def _wait_for_healthy_capture(timeout_seconds: float = 20.0) -> None:
    deadline = monotonic() + timeout_seconds
    last_problem = "capture did not become ready"
    while monotonic() < deadline:
        problem = capture.health_problem()
        status = capture.status()
        if problem:
            last_problem = problem
            if not status["running"]:
                break
        if (
            status["running"]
            and capture.latest_frame is not None
            and status["audio_active"]
            and int(status["buffered_chunks"]) > 0
        ):
            return
        await asyncio.sleep(0.25)
    raise RuntimeError(last_problem)


async def capture_loop() -> None:
    restart_streak = 0
    first_start = True
    while True:
        if settings_manager.applying:
            await asyncio.sleep(0.25)
            continue
        problem = capture.health_problem()
        if problem is not None:
            restart_streak += 1
            delay = 0.0 if first_start else min(
                capture.settings.restart_max_backoff_seconds,
                float(2 ** min(restart_streak - 1, 5)),
            )
            first_start = False
            if delay:
                LOGGER.warning("Capture reset in %.1fs: %s", delay, problem)
                await asyncio.sleep(delay)
            if settings_manager.applying:
                continue
            try:
                async with capture_control_lock:
                    await capture.restart(problem)
            except Exception:
                LOGGER.exception("Failed to start a clean capture generation")
        else:
            await capture.cleanup_old_chunks()
            if (capture.generation_age_seconds() or 0) > 30:
                restart_streak = 0
        await asyncio.sleep(1)


async def thumbnail_loop() -> None:
    while True:
        generated = await asyncio.to_thread(catalog.generate_missing_thumbnail)
        await asyncio.sleep(0.25 if generated else 15)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.app_password:
        raise RuntimeError("APP_PASSWORD must be set in .env")
    if len(settings.session_secret) < 32:
        raise RuntimeError("SESSION_SECRET must be at least 32 characters in .env")
    for directory in (
        settings.data_dir,
        settings.chunk_dir,
        settings.replay_dir,
        settings.work_dir,
        settings.trash_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(catalog.import_existing)

    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()

    def handle_asyncio_exception(event_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        if _is_expected_client_disconnect(context):
            logging.getLogger("sigit.http").debug("Browser closed a streaming connection")
            return
        if previous_exception_handler is not None:
            previous_exception_handler(event_loop, context)
        else:
            event_loop.default_exception_handler(context)

    loop.set_exception_handler(handle_asyncio_exception)
    capture_task = asyncio.create_task(capture_loop())
    thumbnails_task = asyncio.create_task(thumbnail_loop())
    try:
        yield
    finally:
        for task in (capture_task, thumbnails_task):
            task.cancel()
        await asyncio.gather(capture_task, thumbnails_task, return_exceptions=True)
        await capture.stop()
        loop.set_exception_handler(previous_exception_handler)


app = FastAPI(title="Sigit Live", lifespan=lifespan)
app.add_middleware(PasswordAuthMiddleware, settings=settings)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if is_authenticated(request, settings):
        return RedirectResponse(safe_next_path(next), status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None, "next": safe_next_path(next)},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, password: str = Form(...), next: str = Form("/")):
    client = request.client.host if request.client else "unknown"
    target = safe_next_path(next)
    if login_limiter.is_limited(client):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Too many attempts. Try again in a few minutes.", "next": target},
            status_code=429,
            headers={"Cache-Control": "no-store"},
        )
    if not hmac.compare_digest(password, settings.app_password):
        login_limiter.record_failure(client)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Incorrect password.", "next": target},
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )
    login_limiter.clear(client)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        session_token(settings),
        max_age=max(settings.auth_session_hours, 1) * 3600,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        path="/",
    )
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    current = settings_manager.current
    return templates.TemplateResponse(
        "index.html",
        _template_context(
            request,
            replay_presets=list(current.replay_presets_seconds),
            default_replay_seconds=current.default_replay_seconds,
        ),
    )


@app.get("/highlights", response_class=HTMLResponse)
async def highlights(request: Request):
    return templates.TemplateResponse("highlights.html", _template_context(request))


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", _template_context(request))


@app.get("/api/status")
async def status():
    current = settings_manager.current
    capture_status = capture.status()
    frame_age = capture_status["live_frame_age_seconds"]
    live_ready = bool(
        capture_status["running"]
        and capture.latest_frame is not None
        and (frame_age is None or float(frame_age) < 5)
    )
    warning = None
    if not capture_status["running"]:
        warning = capture_status["last_error"] or "Camera capture is not running"
    elif not live_ready:
        warning = "Camera capture is running, waiting for fresh live frames"
    return {
        "app_version": APP_VERSION,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "capture_running": capture_status["running"],
        "capture": capture_status,
        "live_mode": "mjpeg",
        "live_url": "/live.mjpg",
        "live_ready": live_ready,
        "replay_presets_seconds": list(current.replay_presets_seconds),
        "default_replay_seconds": current.default_replay_seconds,
        "max_buffer_minutes": current.max_buffer_minutes,
        "buffered_chunks": capture_status["buffered_chunks"],
        "buffered_seconds_estimate": capture_status["buffered_duration_seconds"],
        "stream_warning": warning,
        "save_in_progress": replay_task is not None and not replay_task.done(),
        "settings_apply_in_progress": settings_manager.applying,
        "ffmpeg_path": discover_ffmpeg_path() or current.ffmpeg_path,
        "ffmpeg_error": ffmpeg_discovery_error(),
        "backup": {
            "configured": current.replay_backup_dir is not None,
            "path": str(current.replay_backup_dir) if current.replay_backup_dir else None,
        },
    }


@app.get("/api/devices")
async def devices():
    current = settings_manager.current
    ffmpeg_path = discover_ffmpeg_path() or current.ffmpeg_path
    inventory = await asyncio.to_thread(list_dshow_devices, ffmpeg_path)
    capture.devices = inventory
    return {
        "video": inventory.video,
        "audio": inventory.audio,
        "error": inventory.error,
        "ffmpeg_path": ffmpeg_path,
        "ffmpeg_error": ffmpeg_discovery_error(),
    }


@app.get("/api/audio-level")
async def audio_level():
    capture_status = capture.status()
    return {
        "peak_db": capture_status["audio_peak_db"],
        "age_seconds": capture_status["audio_level_age_seconds"],
        "active": capture_status["audio_active"],
        "microphone": capture_status["selected_audio_device"],
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
                            capture.frame_condition.wait_for(
                                lambda: capture.frame_count != last_count or not capture.is_running()
                            ),
                            timeout=10,
                        )
                    except asyncio.TimeoutError:
                        break
                    if capture.latest_frame is None:
                        continue
                    frame = capture.latest_frame
                    last_count = capture.frame_count
                yield b"--frame\r\nContent-Type: image/jpeg\r\nCache-Control: no-store\r\n\r\n" + frame + b"\r\n"
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except (ConnectionResetError, BrokenPipeError):
            pass
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
async def create_replay(request: Request):
    global replay_task
    if settings_manager.applying:
        raise HTTPException(status_code=409, detail="Capture settings are being applied")
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    current = settings_manager.current
    try:
        seconds = int(payload.get("seconds", current.default_replay_seconds))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Replay duration must be an integer") from exc
    if seconds not in current.replay_presets_seconds:
        raise HTTPException(status_code=422, detail="Replay duration is not an enabled preset")

    async with replay_lock:
        if replay_task is not None and not replay_task.done():
            raise HTTPException(status_code=409, detail="A replay is already being saved", headers={"Retry-After": "2"})
        replay_task = asyncio.create_task(save_replay(current, capture, seconds=seconds))
    try:
        result = await replay_task
        record = await asyncio.to_thread(catalog.register, result)
        backup_status, backup_error = await asyncio.to_thread(catalog.backup_new_pair, str(record["id"]))
        record = await asyncio.to_thread(catalog.get, str(record["id"]))
        record["backup_status"] = backup_status
        record["backup_error"] = backup_error
        return record
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/replays")
async def list_replays(q: str = "", tag: str = "", favorite: bool | None = None):
    return await asyncio.to_thread(catalog.list_replays, q, tag, favorite)


@app.patch("/api/replays/{replay_id}")
async def update_replay(replay_id: str, request: Request):
    try:
        payload = await request.json()
        return await asyncio.to_thread(catalog.update_metadata, replay_id, payload)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Replay not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/replays/{replay_id}/trash")
async def trash_replay(replay_id: str):
    try:
        return await asyncio.to_thread(catalog.trash, replay_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Replay not found") from exc
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/trash")
async def list_trash():
    return await asyncio.to_thread(catalog.list_trash)


@app.post("/api/trash/{replay_id}/restore")
async def restore_replay(replay_id: str):
    try:
        return await asyncio.to_thread(catalog.restore, replay_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Trash item not found") from exc
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/api/trash/{replay_id}")
async def delete_replay_permanently(replay_id: str, request: Request):
    try:
        payload = await request.json()
        await asyncio.to_thread(catalog.permanently_delete, replay_id, str(payload.get("confirm", "")))
        return Response(status_code=204)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Trash item not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/storage")
async def storage_status():
    return await asyncio.to_thread(catalog.storage_status)


@app.get("/api/settings")
async def get_settings():
    return {
        "values": settings_manager.public_values(),
        "capture_reset_fields": sorted(CAPTURE_RESET_FIELDS),
        "applying": settings_manager.applying,
    }


@app.put("/api/settings")
async def apply_settings(request: Request):
    global replay_task
    if settings_manager.lock.locked():
        raise HTTPException(status_code=409, detail="Another settings update is in progress")
    if replay_task is not None and not replay_task.done():
        raise HTTPException(status_code=409, detail="Wait for the current replay save to finish")
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON") from exc
    values = payload.get("values")
    if not isinstance(values, dict):
        raise HTTPException(status_code=422, detail="values must be an object")

    async with settings_manager.lock:
        try:
            candidate, changed = settings_manager.candidate(values)
        except (TypeError, ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        requires_reset = bool(changed & CAPTURE_RESET_FIELDS)
        if requires_reset and payload.get("confirm_buffer_reset") is not True:
            raise HTTPException(
                status_code=409,
                detail={"code": "buffer_reset_required", "fields": sorted(changed & CAPTURE_RESET_FIELDS)},
            )
        if not changed:
            return {"values": settings_manager.public_values(), "capture_reset": False, "generation": capture.generation}

        previous = settings_manager.current
        settings_manager.applying = True
        try:
            if requires_reset:
                async with capture_control_lock:
                    capture.settings = candidate
                    try:
                        await capture.restart("settings update")
                        await _wait_for_healthy_capture()
                    except Exception:
                        capture.settings = previous
                        await capture.restart("settings rollback")
                        raise
            await asyncio.to_thread(settings_manager.persist, candidate)
            settings_manager.activate(candidate)
            capture.settings = candidate
            catalog.update_settings(candidate)
        except Exception as exc:
            if requires_reset and capture.settings is not previous:
                async with capture_control_lock:
                    capture.settings = previous
                    await capture.restart("settings persistence rollback")
            raise HTTPException(status_code=409, detail=f"Settings were rolled back: {exc}") from exc
        finally:
            settings_manager.applying = False
        return {
            "values": settings_manager.public_values(),
            "capture_reset": requires_reset,
            "generation": capture.generation,
        }


def _test_backup_path(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    probe = path / f".sigit-write-test-{uuid4().hex}.tmp"
    try:
        probe.write_bytes(b"Sigit Live")
        if probe.read_bytes() != b"Sigit Live":
            raise OSError("Backup write verification failed")
    finally:
        probe.unlink(missing_ok=True)


@app.post("/api/settings/test-backup")
async def test_backup_path():
    path = settings_manager.current.replay_backup_dir
    if path is None:
        raise HTTPException(status_code=422, detail="Configure a backup path first")
    try:
        await asyncio.to_thread(_test_backup_path, path)
        return {"ok": True, "path": str(path)}
    except OSError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/replays/{filename}")
async def download_replay(filename: str):
    path = settings_manager.current.replay_dir / filename
    if path.suffix.lower() != ".mp4" or not path.is_file() or path.parent != settings_manager.current.replay_dir:
        raise HTTPException(status_code=404, detail="Replay not found")
    return FileResponse(path, media_type="video/mp4")


@app.get("/thumbnails/{filename}")
async def replay_thumbnail(filename: str):
    path = settings_manager.current.replay_dir / filename
    if path.suffix.lower() != ".jpg" or not path.is_file() or path.parent != settings_manager.current.replay_dir:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
