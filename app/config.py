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


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = _int_env("PORT", 8000)

    input_mode: str = os.getenv("INPUT_MODE", "dshow").lower()
    auto_detect_devices: bool = os.getenv("AUTO_DETECT_DEVICES", "1").lower() not in {"0", "false", "no"}
    video_device: str = os.getenv("VIDEO_DEVICE", "")
    audio_device: str = os.getenv("AUDIO_DEVICE", "")
    rtsp_url: str = os.getenv("RTSP_URL", "")

    replay_minutes: int = _int_env("REPLAY_MINUTES", 3)
    max_buffer_minutes: int = _int_env("MAX_BUFFER_MINUTES", 5)
    chunk_seconds: int = _int_env("CHUNK_SECONDS", 5)
    video_resolution: str = os.getenv("VIDEO_RESOLUTION", "1920x1080")
    fps: int = _int_env("FPS", 30)
    video_codec: str = os.getenv("VIDEO_CODEC", "libx264")
    audio_codec: str = os.getenv("AUDIO_CODEC", "aac")
    ffmpeg_path: str = os.getenv("FFMPEG_PATH", "")
    live_fps: int = _int_env("LIVE_FPS", 8)
    live_width: int = _int_env("LIVE_WIDTH", 960)
    live_jpeg_quality: int = _int_env("LIVE_JPEG_QUALITY", 8)
    dshow_rtbufsize: str = os.getenv("DSHOW_RTBUFSIZE", "512M")

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
