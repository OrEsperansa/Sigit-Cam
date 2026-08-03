from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


def _path_env(name: str) -> Path | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return Path(value)


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.lower() not in {"0", "false", "no"}


def _int_tuple_env(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    return tuple(int(item.strip()) for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = _int_env("PORT", 8000)
    app_password: str = os.getenv("APP_PASSWORD", "")
    session_secret: str = os.getenv("SESSION_SECRET", "")
    auth_cookie_secure: bool = _bool_env("AUTH_COOKIE_SECURE", False)
    auth_session_hours: int = _int_env("AUTH_SESSION_HOURS", 12)

    input_mode: str = os.getenv("INPUT_MODE", "dshow").lower()
    video_device: str = os.getenv("VIDEO_DEVICE", "")
    audio_device: str = os.getenv("AUDIO_DEVICE", "")

    replay_minutes: int = _int_env("REPLAY_MINUTES", 3)
    replay_presets_seconds: tuple[int, ...] = _int_tuple_env("REPLAY_PRESETS_SECONDS", (30, 60, 180))
    default_replay_seconds: int = _int_env("DEFAULT_REPLAY_SECONDS", _int_env("REPLAY_MINUTES", 3) * 60)
    max_buffer_minutes: int = _int_env("MAX_BUFFER_MINUTES", 5)
    chunk_seconds: int = _int_env("CHUNK_SECONDS", 2)
    video_resolution: str = os.getenv("VIDEO_RESOLUTION", "1280x720")
    camera_rotation_degrees: float = _float_env("CAMERA_ROTATION_DEGREES", 0.0)
    fps: int = _int_env("FPS", 30)
    # Negative values advance late audio; positive values delay early audio.
    audio_sync_offset_ms: int = _int_env("AUDIO_SYNC_OFFSET_MS", 0)
    ffmpeg_path: str = os.getenv("FFMPEG_PATH", "")
    live_fps: int = _int_env("LIVE_FPS", 8)
    live_width: int = _int_env("LIVE_WIDTH", 960)
    live_jpeg_quality: int = _int_env("LIVE_JPEG_QUALITY", 8)
    dshow_rtbufsize: str = os.getenv("DSHOW_RTBUFSIZE", "256M")
    audio_startup_grace_seconds: float = _float_env("AUDIO_STARTUP_GRACE_SECONDS", 10.0)
    audio_stall_seconds: float = _float_env("AUDIO_STALL_SECONDS", 3.0)
    video_stall_seconds: float = _float_env("VIDEO_STALL_SECONDS", 10.0)
    restart_max_backoff_seconds: float = _float_env("RESTART_MAX_BACKOFF_SECONDS", 30.0)
    replay_backup_dir: Path | None = _path_env("REPLAY_BACKUP_DIR")

    data_dir: Path = BASE_DIR / "data"
    chunk_dir: Path = BASE_DIR / "data" / "chunks"
    replay_dir: Path = BASE_DIR / "data" / "replays"
    work_dir: Path = BASE_DIR / "data" / "work"
    trash_dir: Path = BASE_DIR / "data" / "trash"

    @property
    def replay_seconds(self) -> int:
        return self.default_replay_seconds

    @property
    def max_buffer_seconds(self) -> int:
        return self.max_buffer_minutes * 60

    def validate_capture(self) -> None:
        if self.input_mode != "dshow":
            raise RuntimeError("INPUT_MODE must be dshow on the Windows capture host")
        if not self.video_device.strip() or not self.audio_device.strip():
            raise RuntimeError("VIDEO_DEVICE and AUDIO_DEVICE must be exact DirectShow device names")
        if not self.replay_presets_seconds or any(seconds <= 0 for seconds in self.replay_presets_seconds):
            raise RuntimeError("REPLAY_PRESETS_SECONDS must contain positive durations")
        if tuple(sorted(set(self.replay_presets_seconds))) != self.replay_presets_seconds:
            raise RuntimeError("REPLAY_PRESETS_SECONDS must be unique and sorted")
        if self.default_replay_seconds not in self.replay_presets_seconds:
            raise RuntimeError("DEFAULT_REPLAY_SECONDS must be one of REPLAY_PRESETS_SECONDS")
        if self.max_buffer_seconds < max(self.replay_presets_seconds):
            raise RuntimeError("MAX_BUFFER_MINUTES must cover the longest replay preset")
        if self.chunk_seconds <= 0:
            raise RuntimeError("CHUNK_SECONDS must be positive")
        if self.fps <= 0 or self.live_fps <= 0 or self.live_width <= 0:
            raise RuntimeError("FPS, LIVE_FPS, and LIVE_WIDTH must be positive")
        if self.audio_startup_grace_seconds <= 0 or self.audio_stall_seconds <= 0:
            raise RuntimeError("Audio watchdog intervals must be positive")
        if self.video_stall_seconds <= 0 or self.restart_max_backoff_seconds <= 0:
            raise RuntimeError("Video watchdog and restart backoff intervals must be positive")
        if not 2 <= self.live_jpeg_quality <= 31:
            raise RuntimeError("LIVE_JPEG_QUALITY must be between 2 and 31")


settings = Settings()
