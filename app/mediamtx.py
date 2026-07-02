from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
from pathlib import Path

from .config import Settings


LOGGER = logging.getLogger("instant_replay.mediamtx")


class MediaMTXProcess:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.process: subprocess.Popen[str] | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return

        exe = Path(self.settings.mediamtx_path)
        config = Path(self.settings.mediamtx_config)
        if not exe.is_file():
            self.last_error = f"MediaMTX executable not found: {exe}"
            raise RuntimeError(self.last_error)
        if not config.is_file():
            self.last_error = f"MediaMTX config not found: {config}"
            raise RuntimeError(self.last_error)

        command = [str(exe), str(config)]
        LOGGER.info("Starting MediaMTX: %s", " ".join(command))
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(exe.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            self.last_error = None
        except OSError as exc:
            self.last_error = f"MediaMTX could not start: {exc}"
            raise RuntimeError(self.last_error) from exc
        asyncio.create_task(self._log_stderr())

    def stop(self) -> None:
        if not self.process or self.process.poll() is not None:
            return

        LOGGER.info("Stopping MediaMTX")
        if os.name == "nt":
            self.process.send_signal(signal.CTRL_BREAK_EVENT)
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
            "last_error": self.last_error,
            "path": self.settings.mediamtx_path,
            "config": self.settings.mediamtx_config,
        }

    async def _log_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return
        while True:
            line = await asyncio.to_thread(self.process.stderr.readline)
            if not line:
                break
            LOGGER.info(line.rstrip())
