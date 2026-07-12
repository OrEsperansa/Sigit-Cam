from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from datetime import datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from .backup import copy_replay_atomic
from .config import BASE_DIR, Settings


LOGGER = logging.getLogger("instant_replay.ffmpeg")


@dataclass(frozen=True)
class DeviceInventory:
    video: list[str] = field(default_factory=list)
    audio: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class ReplaySaveResult:
    output: Path
    backup_path: Path | None = None
    backup_error: str | None = None


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


def list_dshow_devices(ffmpeg_path: str) -> DeviceInventory:
    if not ffmpeg_path:
        return DeviceInventory(error=ffmpeg_discovery_error() or "No usable FFmpeg executable was found")

    command = [
        ffmpeg_path,
        "-hide_banner",
        "-list_devices",
        "true",
        "-f",
        "dshow",
        "-i",
        "dummy",
    ]

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        discovered = discover_ffmpeg_path()
        if discovered and discovered != ffmpeg_path:
            return list_dshow_devices(discovered)
        return DeviceInventory(error=f"{ffmpeg_path!r} was not found on PATH")
    except OSError as exc:
        discovered = discover_ffmpeg_path()
        if discovered and discovered != ffmpeg_path:
            return list_dshow_devices(discovered)
        return DeviceInventory(error=f"{ffmpeg_path!r} could not run: {exc}")
    except subprocess.TimeoutExpired:
        return DeviceInventory(error="FFmpeg device detection timed out")

    video: list[str] = []
    audio: list[str] = []
    section: str | None = None
    device_pattern = re.compile(r'"([^"]+)"')

    for line in result.stderr.splitlines():
        lower = line.lower()
        if "directshow video devices" in lower:
            section = "video"
            continue
        if "directshow audio devices" in lower:
            section = "audio"
            continue

        match = device_pattern.search(line)
        if not match or not section:
            continue
        name = match.group(1)
        if name.startswith("@device_"):
            continue
        if section == "video":
            video.append(name)
        else:
            audio.append(name)

    error = None if video else "No DirectShow video devices were detected"
    return DeviceInventory(video=video, audio=audio, error=error)


@lru_cache(maxsize=1)
def discover_ffmpeg_path() -> str | None:
    for candidate, error in _ffmpeg_candidates():
        if error is None:
            return str(candidate)
        LOGGER.warning("Skipping unusable FFmpeg candidate %s: %s", candidate, error)
    return None


def _ffmpeg_candidates() -> list[tuple[Path, str | None]]:
    local_app_data = Path(os.getenv("LOCALAPPDATA", ""))
    program_files = Path(os.getenv("ProgramFiles", ""))
    program_files_x86 = Path(os.getenv("ProgramFiles(x86)", ""))
    user_profile = Path(os.getenv("USERPROFILE", ""))
    env_path = os.getenv("FFMPEG_PATH", "")
    path_match = shutil.which("ffmpeg")

    candidates: list[Path] = []
    candidates.append(BASE_DIR / "ffmpeg" / "ffmpeg.exe")
    if env_path:
        candidates.append(Path(env_path))
    if path_match:
        candidates.append(Path(path_match))
    candidates.extend(
        [
            program_files / "Gyan" / "FFmpeg" / "bin" / "ffmpeg.exe",
            program_files / "ffmpeg" / "bin" / "ffmpeg.exe",
            program_files_x86 / "Gyan" / "FFmpeg" / "bin" / "ffmpeg.exe",
            local_app_data / "Microsoft" / "WinGet" / "Packages" / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe" / "ffmpeg-8.0-full_build" / "bin" / "ffmpeg.exe",
            local_app_data / "Programs" / "LNV" / "Stremio-4" / "ffmpeg.exe",
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
    except OSError as exc:
        return str(exc)
    except subprocess.TimeoutExpired:
        return "timed out while running -version"

    if result.returncode != 0:
        output = result.stderr.strip() or result.stdout.strip()
        return output or f"exited with code {result.returncode}"
    return None


class CaptureProcess:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.process: subprocess.Popen[str] | None = None
        self.selected_video_device: str | None = None
        self.selected_audio_device: str | None = None
        self.devices = DeviceInventory()
        self.last_error: str | None = None
        self.latest_frame: bytes | None = None
        self.latest_frame_at: float | None = None
        self.frame_count = 0
        self.frame_condition = asyncio.Condition()
        self.live_clients = 0
        self.session_id: str | None = None
        self._stderr_tail: list[str] = []
        self.corrupt_frame_count = 0
        self._corrupt_since_summary = 0
        self._last_corrupt_summary_at = monotonic()

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return

        self._ensure_dirs()
        self.latest_frame = None
        self.latest_frame_at = None
        self.frame_count = 0
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
        self._stderr_tail = []
        self._corrupt_since_summary = 0
        self._last_corrupt_summary_at = monotonic()
        try:
            command = self._build_command()
        except Exception as exc:
            self.last_error = str(exc)
            raise

        LOGGER.info("Starting FFmpeg: %s", " ".join(command))
        try:
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            self.last_error = None
        except FileNotFoundError as exc:
            discovered = discover_ffmpeg_path()
            if discovered and discovered != command[0]:
                command[0] = discovered
                self.process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                )
                self.last_error = None
                asyncio.create_task(self._log_stderr())
                asyncio.create_task(self._read_mjpeg_stdout())
                return
            self.last_error = f"{self.settings.ffmpeg_path!r} was not found on PATH"
            raise RuntimeError(self.last_error) from exc
        asyncio.create_task(self._log_stderr())
        asyncio.create_task(self._read_mjpeg_stdout())

    def stop(self) -> None:
        if not self.process or self.process.poll() is not None:
            return

        LOGGER.info("Stopping FFmpeg")
        if os.name == "nt":
            self.process.terminate()
        else:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def status(self) -> dict[str, object]:
        return {
            "running": self.is_running(),
            "selected_video_device": self.selected_video_device,
            "selected_audio_device": self.selected_audio_device,
            "available_video_devices": self.devices.video,
            "available_audio_devices": self.devices.audio,
            "device_error": self.devices.error,
            "last_error": self.last_error,
            "live_frame_count": self.frame_count,
            "live_frame_age_seconds": self.live_frame_age_seconds(),
            "live_clients": self.live_clients,
            "corrupt_frame_count": self.corrupt_frame_count,
        }

    def live_frame_age_seconds(self) -> float | None:
        if self.latest_frame_at is None:
            return None
        return round(monotonic() - self.latest_frame_at, 2)

    @staticmethod
    def _is_corrupt_frame_message(message: str) -> bool:
        lower = message.lower()
        return any(pattern in lower for pattern in (
            "bad vlc",
            "dc error",
            "eoi missing",
            "found eoi before any sof",
            "no jpeg data found",
            "error submitting packet to decoder",
            "invalid data found when processing input",
            "overread",
        ))

    def _flush_corrupt_summary(self, force: bool = False) -> None:
        if not self._corrupt_since_summary:
            return
        now = monotonic()
        if not force and now - self._last_corrupt_summary_at < 60:
            return
        LOGGER.warning(
            "Discarded %d corrupt camera JPEG frame(s) in the last interval (%d total)",
            self._corrupt_since_summary,
            self.corrupt_frame_count,
        )
        self._corrupt_since_summary = 0
        self._last_corrupt_summary_at = now

    async def _log_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return
        while True:
            line = await asyncio.to_thread(self.process.stderr.readline)
            if not line:
                break
            message = line.rstrip()
            if self._is_corrupt_frame_message(message):
                self.corrupt_frame_count += 1
                self._corrupt_since_summary += 1
                self._flush_corrupt_summary()
                continue
            LOGGER.info(message)
            self._stderr_tail.append(message)
            self._stderr_tail = self._stderr_tail[-20:]
        self._flush_corrupt_summary(force=True)
        if self.process and self._stderr_tail:
            return_code = await asyncio.to_thread(self.process.wait)
            if return_code != 0:
                self.last_error = self._stderr_tail[-1]
    async def _read_mjpeg_stdout(self) -> None:
        if not self.process or not self.process.stdout:
            return

        buffer = bytearray()
        while True:
            chunk = await asyncio.to_thread(self.process.stdout.buffer.read, 8192)
            if not chunk:
                break
            buffer.extend(chunk)

            while True:
                start = buffer.find(b"\xff\xd8")
                end = buffer.find(b"\xff\xd9", start + 2)
                if start == -1:
                    buffer.clear()
                    break
                if end == -1:
                    if start > 0:
                        del buffer[:start]
                    break

                frame = bytes(buffer[start : end + 2])
                del buffer[: end + 2]
                async with self.frame_condition:
                    self.latest_frame = frame
                    self.latest_frame_at = monotonic()
                    self.frame_count += 1
                    self.frame_condition.notify_all()

            if len(buffer) > 2_000_000:
                del buffer[:-2]

    def _ensure_dirs(self) -> None:
        self.settings.chunk_dir.mkdir(parents=True, exist_ok=True)
        self.settings.replay_dir.mkdir(parents=True, exist_ok=True)

    def _build_command(
        self,
        *,
        input_args: list[str] | None = None,
        audio_map: str | None = None,
    ) -> list[str]:
        input_args = self._input_args() if input_args is None else input_args
        audio_map = self._audio_map() if audio_map is None else audio_map
        keyframe_interval = max(self.settings.fps * self.settings.chunk_seconds, 1)
        if self.session_id is None:
            self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
        # Numeric segment names are understood directly by the segment muxer.
        # A unique session prefix prevents collisions with previous app runs.
        chunk_pattern = self.settings.chunk_dir / f"chunk_{self.session_id}_%06d.mp4"

        return [
            require_ffmpeg_path(),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            *input_args,
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            self._live_video_filter(),
            "-q:v",
            str(self.settings.live_jpeg_quality),
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-fps_mode",
            "passthrough",
            "pipe:1",
            "-map",
            "0:v:0",
            "-map",
            audio_map,
            *self._recording_video_filter_args(),
            *self._encoder_args(),
            "-fps_mode",
            "cfr",
            "-g",
            str(keyframe_interval),
            "-sc_threshold",
            "0",
            "-af",
            "aresample=async=1000:first_pts=0,asetpts=PTS-STARTPTS",
            "-c:a",
            self.settings.audio_codec,
            "-b:a",
            "128k",
            "-f",
            "segment",
            "-segment_time",
            str(self.settings.chunk_seconds),
            "-reset_timestamps",
            "1",
            "-segment_format",
            "mp4",
            str(chunk_pattern),
        ]

    def _live_video_filter(self) -> str:
        filters = []
        rotation = self._rotation_filter()
        if rotation:
            filters.append(rotation)
        filters.extend([
            f"fps={self.settings.live_fps}",
            f"scale={self.settings.live_width}:-2",
        ])
        return ",".join(filters)

    def _recording_video_filter_args(self) -> list[str]:
        filters = ["setpts=PTS-STARTPTS"]
        rotation = self._rotation_filter()
        if rotation:
            filters.append(rotation)
        return ["-vf", ",".join(filters)]

    def _rotation_filter(self) -> str:
        degrees = self.settings.camera_rotation_degrees % 360
        if abs(degrees) < 0.0001:
            return ""
        if abs(degrees - 90) < 0.0001:
            return "transpose=clock"
        if abs(degrees - 180) < 0.0001:
            return "hflip,vflip"
        if abs(degrees - 270) < 0.0001:
            return "transpose=cclock"
        return f"rotate={degrees}*PI/180:ow=ceil(rotw(iw)/2)*2:oh=ceil(roth(ih)/2)*2:fillcolor=black"

    def _encoder_args(self) -> list[str]:
        codec = self.settings.video_codec.lower()
        args = ["-c:v", self.settings.video_codec, "-preset", "veryfast"]
        if codec in {"libx264", "libx265"}:
            args.extend(["-tune", "zerolatency"])
        args.extend(["-pix_fmt", self._video_pixel_format()])
        return args

    def _video_pixel_format(self) -> str:
        configured = self.settings.video_pixel_format
        if configured and configured != "auto":
            return configured
        if self.settings.video_codec.lower() == "h264_qsv":
            return "nv12"
        return "yuv420p"

    def _input_args(self) -> list[str]:
        mode = self.settings.input_mode
        if mode == "rtsp":
            if not self.settings.rtsp_url:
                raise RuntimeError("RTSP_URL is required when INPUT_MODE=rtsp")
            return [
                *self._low_latency_input_args(),
                "-rtsp_transport",
                self.settings.rtsp_transport,
                "-i",
                self.settings.rtsp_url,
            ]

        if mode == "dshow":
            video_device, audio_device = self._resolve_dshow_devices()
            source = f"video={video_device}"
            if audio_device:
                source += f":audio={audio_device}"
            return [
                *self._low_latency_input_args(),
                "-use_wallclock_as_timestamps",
                "1",
                "-f",
                "dshow",
                "-rtbufsize",
                self.settings.dshow_rtbufsize,
                "-video_size",
                self.settings.video_resolution,
                "-framerate",
                str(self.settings.fps),
                "-i",
                source,
            ]

        if mode == "v4l2":
            video = self.settings.video_device or "/dev/video0"
            args = [
                *self._low_latency_input_args(),
                "-f",
                "v4l2",
                "-video_size",
                self.settings.video_resolution,
                "-framerate",
                str(self.settings.fps),
                "-i",
                video,
            ]
            if self.settings.audio_device:
                args.extend(["-f", "alsa", "-i", self.settings.audio_device])
            return args

        raise RuntimeError(f"Unsupported INPUT_MODE={mode!r}")

    def _low_latency_input_args(self) -> list[str]:
        if not self.settings.low_latency_capture:
            return []
        return [
            "-fflags",
            "+nobuffer+discardcorrupt",
            "-flags",
            "low_delay",
            "-probesize",
            "32",
            "-analyzeduration",
            "0",
        ]

    def _audio_map(self) -> str:
        if self.settings.input_mode == "v4l2" and self.settings.audio_device:
            return "1:a:0?"
        return "0:a:0?"

    def _resolve_dshow_devices(self) -> tuple[str, str | None]:
        if self.settings.video_device:
            self.selected_video_device = self.settings.video_device
            self.selected_audio_device = self.settings.audio_device or None
            return self.settings.video_device, self.settings.audio_device or None

        if not self.settings.auto_detect_devices:
            raise RuntimeError("VIDEO_DEVICE is required when AUTO_DETECT_DEVICES=0")

        self.devices = list_dshow_devices(discover_ffmpeg_path() or self.settings.ffmpeg_path)
        if self.devices.error:
            raise RuntimeError(self.devices.error)
        if not self.devices.video:
            raise RuntimeError("No DirectShow video devices were detected")

        self.selected_video_device = self.devices.video[0]
        self.selected_audio_device = self.settings.audio_device or (self.devices.audio[0] if self.devices.audio else None)
        LOGGER.info(
            "Auto-selected DirectShow devices: video=%r audio=%r",
            self.selected_video_device,
            self.selected_audio_device,
        )
        return self.selected_video_device, self.selected_audio_device


async def cleanup_old_chunks(settings: Settings) -> None:
    cutoff = datetime.now().timestamp() - settings.max_buffer_seconds
    for chunk in settings.chunk_dir.glob("chunk_*.mp4"):
        try:
            if chunk.stat().st_mtime < cutoff:
                chunk.unlink()
        except FileNotFoundError:
            continue


def recent_chunks(settings: Settings, seconds: int) -> list[Path]:
    cutoff = datetime.now().timestamp() - seconds
    chunks = [
        path
        for path in settings.chunk_dir.glob("chunk_*.mp4")
        if path.is_file() and path.stat().st_size > 0 and path.stat().st_mtime >= cutoff
    ]
    return sorted(chunks, key=lambda path: path.stat().st_mtime)


def recent_completed_chunks(settings: Settings, seconds: int) -> list[Path]:
    chunks = recent_chunks(settings, seconds)
    if len(chunks) < 2:
        return chunks

    newest = chunks[-1]
    newest_age = datetime.now().timestamp() - newest.stat().st_mtime
    active_cutoff = max(settings.chunk_seconds + 2, 2)
    if newest_age < active_cutoff:
        return chunks[:-1]
    return chunks


async def wait_for_current_chunk_to_finish(settings: Settings) -> None:
    wait_seconds = max(settings.replay_finalize_wait_seconds, 0)
    if wait_seconds == 0:
        return

    chunks = recent_chunks(settings, settings.max_buffer_seconds)
    if not chunks:
        return

    initial_newest = chunks[-1]
    deadline = monotonic() + wait_seconds
    while monotonic() < deadline:
        await asyncio.sleep(0.25)
        current_chunks = recent_chunks(settings, settings.max_buffer_seconds)
        if not current_chunks:
            return
        newest = current_chunks[-1]
        if newest != initial_newest:
            return
        newest_age = datetime.now().timestamp() - newest.stat().st_mtime
        if newest_age >= settings.chunk_seconds + 1:
            return


def _copy_replay_to_backup(settings: Settings, output: Path) -> tuple[Path | None, str | None]:
    if settings.replay_backup_dir is None:
        return None, None
    try:
        return copy_replay_atomic(settings, output), None
    except OSError as exc:
        message = f"Failed to copy replay to backup dir {settings.replay_backup_dir}: {exc}"
        LOGGER.exception(message)
        return None, message

def _replay_concat_command(settings: Settings, concat_file: Path, output: Path) -> list[str]:
    base = [
        require_ffmpeg_path(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "+genpts",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
    ]

    if settings.replay_audio_mode == "copy":
        return [
            *base,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ]

    return [
        *base,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "copy",
        "-c:a",
        settings.audio_codec,
        "-b:a",
        "128k",
        "-af",
        "aresample=async=1:first_pts=0",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        str(output),
    ]

async def save_replay(settings: Settings, seconds: int | None = None) -> ReplaySaveResult:
    duration = seconds or settings.replay_seconds
    await wait_for_current_chunk_to_finish(settings)
    chunks = recent_completed_chunks(settings, duration)
    if not chunks:
        raise RuntimeError("No completed buffered chunks are available yet")

    settings.replay_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    output = settings.replay_dir / f"replay_{stamp}.mp4"
    concat_file = settings.data_dir / f"concat_{stamp}.txt"

    concat_file.write_text(
        "".join(f"file '{chunk.as_posix()}'\n" for chunk in chunks),
        encoding="utf-8",
    )

    command = _replay_concat_command(settings, concat_file, output)


    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace").strip())
        backup_path, backup_error = await asyncio.to_thread(_copy_replay_to_backup, settings, output)
        return ReplaySaveResult(output=output, backup_path=backup_path, backup_error=backup_error)
    finally:
        concat_file.unlink(missing_ok=True)