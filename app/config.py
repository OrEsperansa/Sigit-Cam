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


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = _int_env("PORT", 8000)

    input_mode: str = os.getenv("INPUT_MODE", "dshow").lower()
    auto_detect_devices: bool = os.getenv("AUTO_DETECT_DEVICES", "1").lower() not in {"0", "false", "no"}
    video_device: str = os.getenv("VIDEO_DEVICE", "")
    audio_device: str = os.getenv("AUDIO_DEVICE", "")
    rtsp_url: str = os.getenv("RTSP_URL", "")
    rtsp_transport: str = os.getenv("RTSP_TRANSPORT", "tcp").lower()

    replay_minutes: int = _int_env("REPLAY_MINUTES", 3)
    max_buffer_minutes: int = _int_env("MAX_BUFFER_MINUTES", 5)
    chunk_seconds: int = _int_env("CHUNK_SECONDS", 5)
    video_resolution: str = os.getenv("VIDEO_RESOLUTION", "1280x720")
    camera_rotation_degrees: float = _float_env("CAMERA_ROTATION_DEGREES", 0.0)
    fps: int = _int_env("FPS", 30)
    video_codec: str = os.getenv("VIDEO_CODEC", "libx264")
    video_pixel_format: str = os.getenv("VIDEO_PIXEL_FORMAT", "auto").lower()
    audio_codec: str = os.getenv("AUDIO_CODEC", "aac")
    ffmpeg_path: str = os.getenv("FFMPEG_PATH", "")
    live_fps: int = _int_env("LIVE_FPS", 8)
    live_width: int = _int_env("LIVE_WIDTH", 960)
    live_jpeg_quality: int = _int_env("LIVE_JPEG_QUALITY", 8)
    dshow_rtbufsize: str = os.getenv("DSHOW_RTBUFSIZE", "2M")
    low_latency_capture: bool = os.getenv("LOW_LATENCY_CAPTURE", "1").lower() not in {"0", "false", "no"}
    replay_finalize_wait_seconds: int = _int_env("REPLAY_FINALIZE_WAIT_SECONDS", 7)
    replay_audio_mode: str = os.getenv("REPLAY_AUDIO_MODE", "repair").lower()
    replay_backup_dir: Path | None = _path_env("REPLAY_BACKUP_DIR")

    data_dir: Path = BASE_DIR / "data"
    chunk_dir: Path = BASE_DIR / "data" / "chunks"
    replay_dir: Path = BASE_DIR / "data" / "replays"

    @property
    def replay_seconds(self) -> int:
        return self.replay_minutes * 60

    @property
    def max_buffer_seconds(self) -> int:
        return self.max_buffer_minutes * 60


settings = Settings()
