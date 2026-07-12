from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.backup import BackupSynchronizer, copy_replay_atomic
from app.config import Settings
from app.ffmpeg import CaptureProcess


class BackupSynchronizerTests(unittest.TestCase):
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

    def test_failed_share_is_reported_and_retried_after_recovery(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            settings = self.make_settings(Path(directory))
            source = settings.replay_dir / "replay_pending.mp4"
            source.write_bytes(b"replay")
            settings.replay_backup_dir.write_text("not a directory", encoding="utf-8")
            synchronizer = BackupSynchronizer(settings)

            failed = synchronizer.sync_once()
            self.assertFalse(failed.healthy)
            self.assertEqual(failed.pending_count, 1)
            self.assertIsNotNone(failed.last_error)

            settings.replay_backup_dir.unlink()
            recovered = synchronizer.sync_once()
            self.assertTrue(recovered.healthy)
            self.assertEqual(recovered.pending_count, 0)
            self.assertIsNone(recovered.last_error)
            self.assertEqual((settings.replay_backup_dir / source.name).read_bytes(), b"replay")

    def test_corrupt_jpeg_messages_are_classified(self) -> None:
        settings = replace(Settings(), replay_backup_dir=None)
        capture = CaptureProcess(settings)
        self.assertTrue(capture._is_corrupt_frame_message("EOI missing, emulating"))
        self.assertTrue(capture._is_corrupt_frame_message("bad vlc 0:0"))
        self.assertFalse(capture._is_corrupt_frame_message("Error opening output file"))


if __name__ == "__main__":
    unittest.main()