from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.config import Settings
from app.settings_manager import SettingsManager


class SettingsManagerTests(unittest.TestCase):
    def make_settings(self) -> Settings:
        return replace(
            Settings(),
            video_device="Test Camera",
            audio_device="Test Microphone",
            replay_presets_seconds=(30, 60, 180),
            default_replay_seconds=60,
            max_buffer_minutes=5,
        )

    def test_candidate_validates_presets_and_reports_changed_fields(self) -> None:
        manager = SettingsManager(self.make_settings())
        candidate, changed = manager.candidate(
            {"replay_presets_seconds": [15, 30, 60], "default_replay_seconds": 30}
        )
        self.assertEqual(candidate.replay_presets_seconds, (15, 30, 60))
        self.assertEqual(changed, {"replay_presets_seconds", "default_replay_seconds"})

        with self.assertRaisesRegex(RuntimeError, "must be one of"):
            manager.candidate({"default_replay_seconds": 45})

    def test_persist_atomically_preserves_secrets_comments_and_unknown_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "# Keep this comment\nAPP_PASSWORD=never-touch-me\nCUSTOM_VALUE=keep\nFPS=25\nFPS=24\n",
                encoding="utf-8",
            )
            manager = SettingsManager(self.make_settings(), env_path)
            candidate, _ = manager.candidate({"fps": 30, "replay_backup_dir": "D:\\Replays"})
            manager.persist(candidate)
            content = env_path.read_text(encoding="utf-8")

            self.assertIn("# Keep this comment", content)
            self.assertIn("APP_PASSWORD=never-touch-me", content)
            self.assertIn("CUSTOM_VALUE=keep", content)
            self.assertIn("FPS=30", content)
            self.assertEqual(sum(line.startswith("FPS=") for line in content.splitlines()), 1)
            self.assertIn("REPLAY_BACKUP_DIR=D:\\Replays", content)
            self.assertFalse(list(env_path.parent.glob(".*.tmp")))


if __name__ == "__main__":
    unittest.main()
