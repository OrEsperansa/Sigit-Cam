from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from time import monotonic
from uuid import uuid4

from .backup import copy_replay_atomic
from .config import BASE_DIR, Settings


LOGGER = logging.getLogger("sigit.capture")
TS_PACKET_SIZE = 188


@dataclass(frozen=True)
class DeviceInventory:
    video: list[str] = field(default_factory=list)
    audio: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class ReplaySaveResult:
    output: Path
    requested_seconds: float
    actual_seconds: float
    partial: bool
    reset_generation: int
    backup_path: Path | None = None
    backup_error: str | None = None
    skipped_chunks: tuple[str, ...] = ()


@dataclass(frozen=True)
class MediaValidation:
    valid: bool
    duration_seconds: float = 0.0
    video_frames: int = 0
    audio_samples: int = 0
    error: str | None = None


@dataclass(frozen=True)
class BufferSnapshot:
    workspace: Path
    chunks: tuple[Path, ...]
    generation: int
    requested_seconds: float
    generation_age_seconds: float


@lru_cache(maxsize=1)
def discover_ffmpeg_path() -> str | None:
    for candidate, error in _ffmpeg_candidates():
        if error is None:
            return str(candidate)
        LOGGER.warning("Skipping unusable FFmpeg candidate %s: %s", candidate, error)
    return None


@lru_cache(maxsize=1)
def ffmpeg_discovery_error() -> str | None:
    if discover_ffmpeg_path():
        return None
    candidates = _ffmpeg_candidates()
    if not candidates:
        return "No FFmpeg executable was found"
    errors = [f"{path}: {error}" for path, error in candidates if error]
    return "; ".join(errors) if errors else "No usable FFmpeg executable was found"


def require_ffmpeg_path() -> str:
    path = discover_ffmpeg_path()
    if path:
        return path
    raise RuntimeError(ffmpeg_discovery_error() or "No usable FFmpeg executable was found")


def _ffmpeg_candidates() -> list[tuple[Path, str | None]]:
    local_app_data = Path(os.getenv("LOCALAPPDATA", ""))
    program_files = Path(os.getenv("ProgramFiles", ""))
    program_files_x86 = Path(os.getenv("ProgramFiles(x86)", ""))
    user_profile = Path(os.getenv("USERPROFILE", ""))
    env_path = os.getenv("FFMPEG_PATH", "")
    path_match = shutil.which("ffmpeg")
    candidates = [BASE_DIR / "ffmpeg" / "ffmpeg.exe"]
    if env_path:
        candidates.append(Path(env_path))
    if path_match:
        candidates.append(Path(path_match))
    candidates.extend(
        [
            program_files / "Gyan" / "FFmpeg" / "bin" / "ffmpeg.exe",
            program_files / "ffmpeg" / "bin" / "ffmpeg.exe",
            program_files_x86 / "Gyan" / "FFmpeg" / "bin" / "ffmpeg.exe",
            local_app_data / "Microsoft" / "WinGet" / "Packages"
            / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
            / "ffmpeg-8.0-full_build" / "bin" / "ffmpeg.exe",
            user_profile / "scoop" / "shims" / "ffmpeg.exe",
            Path("C:/ffmpeg/bin/ffmpeg.exe"),
        ]
    )
    seen: set[Path] = set()
    results: list[tuple[Path, str | None]] = []
    for candidate in candidates:
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        results.append((candidate, _validate_ffmpeg(candidate)))
    return results


def _validate_ffmpeg(path: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(path), "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if result.returncode == 0:
        return None
    return result.stderr.strip() or result.stdout.strip() or f"exited with code {result.returncode}"


def list_dshow_devices(ffmpeg_path: str) -> DeviceInventory:
    if not ffmpeg_path:
        return DeviceInventory(error=ffmpeg_discovery_error() or "No usable FFmpeg executable was found")
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return DeviceInventory(error=f"DirectShow device detection failed: {exc}")

    video: list[str] = []
    audio: list[str] = []
    section: str | None = None
    pattern = re.compile(r'"([^"]+)"')
    for line in result.stderr.splitlines():
        lower = line.lower()
        if "directshow video devices" in lower:
            section = "video"
            continue
        if "directshow audio devices" in lower:
            section = "audio"
            continue
        match = pattern.search(line)
        if not match or not section or match.group(1).startswith("@device_"):
            continue
        target = video if section == "video" else audio
        if match.group(1) not in target:
            target.append(match.group(1))
    error = None if video and audio else "An exact DirectShow camera and microphone are required"
    return DeviceInventory(video=video, audio=audio, error=error)


class CaptureProcess:
    """Own one FFmpeg process generation and its rolling replay buffer."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.process: asyncio.subprocess.Process | None = None
        self.generation = 0
        self.session_id: str | None = None
        self.selected_video_device: str | None = None
        self.selected_audio_device: str | None = None
        self.devices = DeviceInventory()
        self.last_error: str | None = None
        self.last_reset_reason: str | None = None
        self.last_reset_at: float | None = None
        self.started_at: float | None = None
        self.last_video_frame_at: float | None = None
        self.last_audio_frame_at: float | None = None
        self.last_chunk_progress_at: float | None = None
        self._last_chunk_signature: tuple[str, int, int] | None = None
        self._stderr_tail: list[str] = []
        self.latest_frame: bytes | None = None
        self.frame_count = 0
        self.frame_condition = asyncio.Condition()
        self.live_clients = 0
        self.audio_peak_db: float | None = None
        self.audio_peak_at: float | None = None
        self.corrupt_frame_count = 0
        self.benign_pixel_warning_count = 0
        self.recording_warning: str | None = None
        self.buffer_lock = asyncio.Lock()
        self._restart_lock = asyncio.Lock()
        self._reader_tasks: dict[int, list[asyncio.Task[None]]] = {}
        self._active_workspaces: set[Path] = set()

    def _dshow_input_args(self) -> list[str]:
        if self.settings.input_mode != "dshow":
            raise RuntimeError("Sigit Live capture supports Windows DirectShow only")
        video = self.settings.video_device.strip()
        audio = self.settings.audio_device.strip()
        if not video or not audio:
            raise RuntimeError("VIDEO_DEVICE and AUDIO_DEVICE must contain exact names from /api/devices")
        if '"' in video or '"' in audio or ":" in video or ":" in audio:
            raise RuntimeError("DirectShow device names cannot contain quotation marks or colons")
        self.selected_video_device = video
        self.selected_audio_device = audio
        # subprocess receives an argv array, not a shell command. Literal quote
        # characters would become part of the DirectShow device name.
        source = f"video={video}:audio={audio}"
        return [
            "-thread_queue_size", "1024",
            "-f", "dshow",
            "-rtbufsize", self.settings.dshow_rtbufsize,
            "-video_size", self.settings.video_resolution,
            "-framerate", str(self.settings.fps),
            "-i", source,
        ]

    def _rotation_filter(self) -> str:
        degrees = self.settings.camera_rotation_degrees % 360
        if abs(degrees) < 0.001:
            return ""
        if abs(degrees - 90) < 0.001:
            return "transpose=clock,"
        if abs(degrees - 180) < 0.001:
            return "hflip,vflip,"
        if abs(degrees - 270) < 0.001:
            return "transpose=cclock,"
        radians = degrees * math.pi / 180
        return f"rotate={radians:.8f}:ow=rotw(iw):oh=roth(ih):fillcolor=black,"

    def _recording_audio_filter(self) -> str:
        filters = ["aresample=async=1000:first_pts=0"]
        offset = self.settings.audio_sync_offset_ms
        if offset < 0:
            filters.append(f"atrim=start={abs(offset) / 1000:.3f}")
        elif offset > 0:
            filters.append(f"adelay={offset}:all=1")
        filters.extend(
            [
                "asetpts=N/SR/TB",
                "astats=metadata=1:reset=1",
                "ametadata=mode=print:key=lavfi.astats.Overall.Peak_level",
            ]
        )
        return ",".join(filters)

    def _build_command(
        self,
        input_args: list[str] | None = None,
        audio_map: str = "0:a:0",
    ) -> list[str]:
        if not self.session_id:
            raise RuntimeError("A capture generation must be assigned before building its command")
        source_args = input_args if input_args is not None else self._dshow_input_args()
        chunk_pattern = self.settings.chunk_dir / f"chunk_{self.session_id}_%06d.ts"
        keyframe_interval = max(self.settings.fps * self.settings.chunk_seconds, 1)
        rotation = self._rotation_filter()
        live_filter = (
            f"{rotation}fps={self.settings.live_fps},"
            f"scale={self.settings.live_width}:-2:flags=fast_bilinear:"
            "in_range=auto:out_range=full,format=yuvj420p,setpts=PTS-STARTPTS"
        )
        recording_filter = f"{rotation}format=yuv420p,setpts=PTS-STARTPTS"
        return [
            require_ffmpeg_path(),
            "-hide_banner", "-loglevel", "info", "-nostats", "-nostdin",
            *source_args,
            "-map", "0:v:0", "-an",
            "-vf", live_filter,
            "-c:v", "mjpeg",
            "-q:v", str(self.settings.live_jpeg_quality),
            "-color_range", "pc",
            "-fps_mode", "passthrough",
            "-f", "image2pipe", "pipe:1",
            "-map", "0:v:0",
            "-map", audio_map,
            "-vf", recording_filter,
            "-af", self._recording_audio_filter(),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
            "-flags", "+cgop",
            "-g", str(keyframe_interval),
            "-keyint_min", str(keyframe_interval),
            "-sc_threshold", "0",
            "-c:a", "aac",
            "-b:a", "160k",
            "-ar", "48000",
            "-ac", "2",
            "-max_interleave_delta", "1000000",
            "-flush_packets", "1",
            "-f", "segment",
            "-segment_time", str(self.settings.chunk_seconds),
            "-segment_format", "mpegts",
            "-segment_format_options", "mpegts_flags=+resend_headers",
            str(chunk_pattern),
        ]

    async def restart(self, reason: str) -> None:
        async with self._restart_lock:
            await self._stop_process()
            async with self.buffer_lock:
                self._clear_ephemeral_files()
                self.generation += 1
                self.session_id = f"g{self.generation:06d}_{uuid4().hex[:8]}"
                self._reset_generation_state(reason)
                try:
                    self.settings.validate_capture()
                    await self._spawn_process()
                except Exception as exc:
                    self.last_error = str(exc)
                    LOGGER.error("Capture generation %s could not start: %s", self.generation, exc)
                    raise

    async def stop(self) -> None:
        async with self._restart_lock:
            await self._stop_process()

    def _reset_generation_state(self, reason: str) -> None:
        now = monotonic()
        self.last_reset_reason = reason
        self.last_reset_at = now
        self.started_at = now
        self.last_video_frame_at = None
        self.last_audio_frame_at = None
        self.last_chunk_progress_at = now
        self._last_chunk_signature = None
        self.latest_frame = None
        self.audio_peak_db = None
        self.audio_peak_at = None
        self.recording_warning = None
        self.last_error = None
        self._stderr_tail.clear()

    async def _spawn_process(self) -> None:
        self.settings.chunk_dir.mkdir(parents=True, exist_ok=True)
        command = self._build_command()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self.process = process
        generation = self.generation
        self._reader_tasks[generation] = [
            asyncio.create_task(self._read_mjpeg(process, generation)),
            asyncio.create_task(self._read_stderr(process, generation)),
        ]
        LOGGER.info(
            "Started capture generation %s using video=%r audio=%r",
            generation,
            self.selected_video_device,
            self.selected_audio_device,
        )

    async def _stop_process(self) -> None:
        process = self.process
        generation = self.generation
        if process is None:
            return
        try:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        finally:
            self.process = None
            tasks = self._reader_tasks.pop(generation, [])
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=2)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    if not task.cancelled():
                        try:
                            task.exception()
                        except (asyncio.CancelledError, OSError):
                            pass
            async with self.frame_condition:
                self.latest_frame = None
                self.frame_condition.notify_all()

    async def _read_mjpeg(
        self,
        process: asyncio.subprocess.Process,
        generation: int,
    ) -> None:
        if process.stdout is None:
            return
        data = bytearray()
        try:
            while self._owns(process, generation):
                block = await process.stdout.read(65536)
                if not block:
                    break
                data.extend(block)
                while True:
                    start = data.find(b"\xff\xd8")
                    if start < 0:
                        if len(data) > 2:
                            del data[:-2]
                        break
                    end = data.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        if start:
                            del data[:start]
                        if len(data) > 16 * 1024 * 1024:
                            del data[:-2]
                        break
                    frame = bytes(data[start:end + 2])
                    del data[:end + 2]
                    if not self._owns(process, generation):
                        return
                    async with self.frame_condition:
                        self.latest_frame = frame
                        self.last_video_frame_at = monotonic()
                        self.frame_count += 1
                        self.frame_condition.notify_all()
        except (asyncio.CancelledError, OSError, ValueError):
            raise

    async def _read_stderr(
        self,
        process: asyncio.subprocess.Process,
        generation: int,
    ) -> None:
        if process.stderr is None:
            return
        try:
            while self._owns(process, generation):
                raw = await process.stderr.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or not self._owns(process, generation):
                    continue
                if self._consume_audio_meter_line(line):
                    continue
                if "[segment @" in line and "] Opening '" in line:
                    continue
                self._stderr_tail.append(line)
                self._stderr_tail = self._stderr_tail[-30:]
                if self._is_corrupt_frame_message(line):
                    self.corrupt_frame_count += 1
                    continue
                if self._is_benign_pixel_format_message(line):
                    self.benign_pixel_warning_count += 1
                    continue
                LOGGER.info("FFmpeg generation %s: %s", generation, line)
        except (asyncio.CancelledError, OSError, ValueError):
            raise

    def _owns(self, process: asyncio.subprocess.Process, generation: int) -> bool:
        return self.process is process and self.generation == generation

    def _consume_audio_meter_line(self, line: str) -> bool:
        if line.startswith("frame:") or ("Parsed_ametadata" in line and " frame:" in line):
            self.last_audio_frame_at = monotonic()
            return True
        marker = "lavfi.astats.Overall.Peak_level="
        if marker not in line:
            return False
        value = line.rsplit("=", 1)[-1].strip()
        self.last_audio_frame_at = monotonic()
        self.audio_peak_at = self.last_audio_frame_at
        try:
            peak = float(value)
            self.audio_peak_db = peak if math.isfinite(peak) else -96.0
        except ValueError:
            self.audio_peak_db = -96.0
        return True

    @staticmethod
    def _is_corrupt_frame_message(line: str) -> bool:
        lower = line.lower()
        patterns = (
            "eoi missing",
            "bad vlc",
            "error dc",
            "error y=",
            "mjpeg_decode_dc",
            "overread",
            "concealing",
        )
        return any(pattern in lower for pattern in patterns)

    @staticmethod
    def _is_benign_pixel_format_message(line: str) -> bool:
        lower = line.lower()
        return "deprecated pixel format used" in lower and "range" in lower

    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @staticmethod
    def _age(timestamp: float | None) -> float | None:
        return None if timestamp is None else max(0.0, monotonic() - timestamp)

    def live_frame_age_seconds(self) -> float | None:
        return self._age(self.last_video_frame_at)

    def generation_age_seconds(self) -> float | None:
        return self._age(self.started_at)

    def health_problem(self) -> str | None:
        if not self.is_running():
            if self.process is not None:
                code = self.process.returncode
                detail = self._stderr_tail[-1] if self._stderr_tail else "no FFmpeg error output"
                return f"FFmpeg exited with code {code}: {detail}"
            return self.last_error or "capture process is not running"
        if self.started_at is None:
            return "capture start time is missing"

        now = monotonic()
        generation_age = now - self.started_at
        video_age = self._age(self.last_video_frame_at)
        if generation_age > self.settings.video_stall_seconds and (
            video_age is None or video_age > self.settings.video_stall_seconds
        ):
            return f"video frames stopped for more than {self.settings.video_stall_seconds:g}s"

        audio_age = self._age(self.last_audio_frame_at)
        if generation_age > self.settings.audio_startup_grace_seconds:
            if audio_age is None:
                return "the microphone produced no audio frames during startup"
            if audio_age > self.settings.audio_stall_seconds:
                return f"audio frames stopped for {audio_age:.1f}s"

        chunks = self.current_chunks()
        if chunks:
            newest = chunks[-1]
            try:
                stat = newest.stat()
                signature = (newest.name, stat.st_size, stat.st_mtime_ns)
            except FileNotFoundError:
                signature = None
            if signature is not None and signature != self._last_chunk_signature:
                self._last_chunk_signature = signature
                self.last_chunk_progress_at = now

        chunk_timeout = max(
            self.settings.audio_startup_grace_seconds,
            self.settings.chunk_seconds * 3 + 2,
        )
        progress_age = self._age(self.last_chunk_progress_at)
        if generation_age > chunk_timeout and (progress_age is None or progress_age > chunk_timeout):
            return f"rolling segment output stopped for more than {chunk_timeout:g}s"
        return None

    def current_chunks(self) -> list[Path]:
        if not self.session_id:
            return []
        return sorted(self.settings.chunk_dir.glob(f"chunk_{self.session_id}_*.ts"))

    async def cleanup_old_chunks(self) -> None:
        keep = max(math.ceil(self.settings.max_buffer_seconds / self.settings.chunk_seconds) + 2, 3)
        async with self.buffer_lock:
            chunks = self.current_chunks()
            for path in chunks[:-keep]:
                path.unlink(missing_ok=True)

    def _clear_ephemeral_files(self) -> None:
        self.settings.chunk_dir.mkdir(parents=True, exist_ok=True)
        self.settings.work_dir.mkdir(parents=True, exist_ok=True)
        for path in self.settings.chunk_dir.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        for path in self.settings.work_dir.iterdir():
            if path in self._active_workspaces:
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)

    async def snapshot_buffer(self, seconds: float) -> BufferSnapshot:
        if seconds <= 0:
            raise RuntimeError("Replay duration must be positive")
        workspace = self.settings.work_dir / f"snapshot_{uuid4().hex}"
        async with self.buffer_lock:
            chunks: list[Path] = []
            for path in self.current_chunks():
                try:
                    if path.is_file() and path.stat().st_size > 0:
                        chunks.append(path)
                except FileNotFoundError:
                    continue
            if not chunks:
                raise RuntimeError("The replay buffer is empty")
            take = max(math.ceil(seconds / self.settings.chunk_seconds), 1)
            selected = chunks[-take:]
            workspace.mkdir(parents=True, exist_ok=False)
            self._active_workspaces.add(workspace)
            snapshots: list[Path] = []
            try:
                for source in selected[:-1]:
                    destination = workspace / source.name
                    try:
                        os.link(source, destination)
                    except OSError:
                        shutil.copy2(source, destination)
                    snapshots.append(destination)

                active_source = selected[-1]
                active_destination = workspace / active_source.name
                packet_bytes = (active_source.stat().st_size // TS_PACKET_SIZE) * TS_PACKET_SIZE
                if packet_bytes:
                    with active_source.open("rb") as source_handle:
                        payload = source_handle.read(packet_bytes)
                    with active_destination.open("wb") as destination_handle:
                        destination_handle.write(payload)
                    snapshots.append(active_destination)
                if not snapshots:
                    raise RuntimeError("The active replay segment has no complete transport-stream packets")
                age = self._age(self.started_at) or 0.0
                return BufferSnapshot(
                    workspace=workspace,
                    chunks=tuple(snapshots),
                    generation=self.generation,
                    requested_seconds=seconds,
                    generation_age_seconds=age,
                )
            except Exception:
                self._active_workspaces.discard(workspace)
                shutil.rmtree(workspace, ignore_errors=True)
                raise

    async def release_snapshot(self, snapshot: BufferSnapshot) -> None:
        async with self.buffer_lock:
            self._active_workspaces.discard(snapshot.workspace)
        shutil.rmtree(snapshot.workspace, ignore_errors=True)

    def buffered_stats(self) -> tuple[int, float]:
        chunks = self.current_chunks()
        if not chunks:
            return 0, 0.0
        estimated = min(
            len(chunks) * self.settings.chunk_seconds,
            self.settings.max_buffer_seconds,
            self._age(self.started_at) or 0.0,
        )
        return len(chunks), max(0.0, estimated)

    def status(self) -> dict[str, object]:
        chunk_count, buffered = self.buffered_stats()
        audio_age = self._age(self.last_audio_frame_at)
        peak_age = self._age(self.audio_peak_at)
        return {
            "running": self.is_running(),
            "generation": self.generation,
            "session_id": self.session_id,
            "selected_video_device": self.selected_video_device,
            "selected_audio_device": self.selected_audio_device,
            "device_error": self.devices.error,
            "last_error": self.last_error,
            "last_reset_reason": self.last_reset_reason,
            "last_reset_age_seconds": self._age(self.last_reset_at),
            "live_frame_age_seconds": self.live_frame_age_seconds(),
            "audio_frame_age_seconds": audio_age,
            "chunk_progress_age_seconds": self._age(self.last_chunk_progress_at),
            "live_frame_count": self.frame_count,
            "audio_peak_db": self.audio_peak_db,
            "audio_level_age_seconds": peak_age,
            "audio_active": audio_age is not None and audio_age <= self.settings.audio_stall_seconds,
            "buffered_chunks": chunk_count,
            "buffered_duration_seconds": round(buffered, 3),
            "corrupt_frame_warnings": self.corrupt_frame_count,
            "pixel_format_warnings": self.benign_pixel_warning_count,
            "recording_warning": self.recording_warning,
            "stderr_tail": tuple(self._stderr_tail[-5:]),
        }

def _combine_transport_stream(snapshot: BufferSnapshot) -> Path:
    combined = snapshot.workspace / "combined.ts"
    with combined.open("wb") as output:
        for chunk in snapshot.chunks:
            with chunk.open("rb") as source:
                shutil.copyfileobj(source, output, length=1024 * 1024)
    return combined


def _run_remux(source: Path, destination: Path, timeout: float) -> None:
    command = [
        require_ffmpeg_path(),
        "-hide_banner",
        "-loglevel", "warning",
        "-fflags", "+genpts+discardcorrupt",
        "-i", str(source),
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "160k",
        "-af", "aresample=async=1000:first_pts=0",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        "-y", str(destination),
    ]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-3000:]
        raise RuntimeError(f"FFmpeg could not create the replay: {detail.strip()}")


def validate_media(path: Path, timeout: float = 180) -> MediaValidation:
    command = [
        require_ffmpeg_path(),
        "-hide_banner",
        "-loglevel", "info",
        "-i", str(path),
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-c:v", "copy",
        "-af",
        "astats=metadata=0:reset=0:measure_perchannel=none:measure_overall=Number_of_samples",
        "-progress", "pipe:1",
        "-nostats",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return MediaValidation(False, error=str(exc))

    frames = 0
    duration = 0.0
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        if key == "frame":
            try:
                frames = max(frames, int(value))
            except ValueError:
                pass
        elif key in {"out_time_us", "out_time_ms"}:
            try:
                duration = max(duration, int(value) / 1_000_000)
            except ValueError:
                pass
    samples = 0
    for match in re.finditer(r"Number of samples:\s*([0-9]+)", result.stderr, re.IGNORECASE):
        samples = max(samples, int(match.group(1)))

    if result.returncode != 0:
        return MediaValidation(False, duration, frames, samples, result.stderr[-2000:].strip())
    if frames <= 0:
        return MediaValidation(False, duration, frames, samples, "Replay contains no video frames")
    if samples <= 0:
        return MediaValidation(False, duration, frames, samples, "Replay contains no decoded audio samples")
    if duration <= 0:
        return MediaValidation(False, duration, frames, samples, "Replay duration is zero")
    return MediaValidation(True, duration, frames, samples)


async def save_replay(
    settings: Settings,
    capture: CaptureProcess,
    seconds: float | None = None,
) -> ReplaySaveResult:
    requested = float(seconds if seconds is not None else settings.replay_seconds)
    snapshot = await capture.snapshot_buffer(requested)
    settings.replay_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output = settings.replay_dir / f"replay_{stamp}.mp4"
    temporary = settings.replay_dir / f".{output.stem}.{uuid4().hex}.partial.mp4"
    try:
        combined = await asyncio.to_thread(_combine_transport_stream, snapshot)
        timeout = max(120.0, requested * 2)
        await asyncio.to_thread(_run_remux, combined, temporary, timeout)
        validation = await asyncio.to_thread(validate_media, temporary, timeout)
        if not validation.valid:
            raise RuntimeError(f"Saved replay validation failed: {validation.error}")
        os.replace(temporary, output)

        backup_path: Path | None = None
        backup_error: str | None = None
        try:
            backup_path = await asyncio.to_thread(copy_replay_atomic, settings, output)
        except OSError as exc:
            backup_error = str(exc)
            LOGGER.error("Replay %s was saved locally but backup failed: %s", output.name, exc)

        return ReplaySaveResult(
            output=output,
            requested_seconds=requested,
            actual_seconds=validation.duration_seconds,
            partial=validation.duration_seconds + 0.5 < requested,
            reset_generation=snapshot.generation,
            backup_path=backup_path,
            backup_error=backup_error,
        )
    finally:
        temporary.unlink(missing_ok=True)
        await capture.release_snapshot(snapshot)
