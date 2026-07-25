from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.backup import copy_replay_atomic
from app.config import Settings
from app.ffmpeg import CaptureProcess


class ReplayBackupTests(unittest.TestCase):
    def make_settings(self, root: Path) -> Settings:
        replay_dir = root / "replays"
        replay_dir.mkdir()
        return replace(
            Settings(),
            data_dir=root,
            chunk_dir=root / "chunks",
            replay_dir=replay_dir,
            replay_backup_dir=root / "share",
        )

    def test_atomic_copy_replaces_size_mismatch_and_cleans_partial(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            settings = self.make_settings(Path(directory))
            source = settings.replay_dir / "replay_test.mp4"
            source.write_bytes(b"complete replay")
            settings.replay_backup_dir.mkdir()
            destination = settings.replay_backup_dir / source.name
            destination.write_bytes(b"bad")

            result = copy_replay_atomic(settings, source)

            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(list(settings.replay_backup_dir.glob("*.partial")), [])

    def test_copy_only_operates_on_the_requested_replay(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            settings = self.make_settings(Path(directory))
            requested = settings.replay_dir / "replay_requested.mp4"
            unrelated = settings.replay_dir / "replay_unrelated.mp4"
            requested.write_bytes(b"requested")
            unrelated.write_bytes(b"unrelated")

            result = copy_replay_atomic(settings, requested)

            self.assertEqual(result, settings.replay_backup_dir / requested.name)
            self.assertEqual(result.read_bytes(), b"requested")
            self.assertFalse((settings.replay_backup_dir / unrelated.name).exists())

    def test_corrupt_jpeg_messages_are_classified(self) -> None:
        settings = replace(Settings(), replay_backup_dir=None)
        capture = CaptureProcess(settings)
        self.assertTrue(capture._is_corrupt_frame_message("EOI missing, emulating"))
        self.assertTrue(capture._is_corrupt_frame_message("bad vlc 0:0"))
        self.assertFalse(capture._is_corrupt_frame_message("Error opening output file"))


if __name__ == "__main__":
    unittest.main()
