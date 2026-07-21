import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from soop_timeline.database import Database
from soop_timeline.services.update_checker import (
    AUTO_UPDATE_CHECK_SETTING,
    UPDATE_MANIFEST_SETTING,
    automatic_update_check_enabled,
    bundled_manifest_url,
    configured_manifest_url,
    is_newer_version,
    parse_update_manifest,
)


class UpdateCheckerTests(unittest.TestCase):
    def test_semantic_versions_are_compared(self):
        self.assertTrue(is_newer_version("0.2.0", "0.1.9"))
        self.assertTrue(is_newer_version("v1.0.0", "0.9.9"))
        self.assertFalse(is_newer_version("1.0.0-beta.1", "1.0.0"))
        self.assertFalse(is_newer_version("1.0", "1.0.0"))
        self.assertFalse(is_newer_version("1.0.0+build.2", "1.0.0+build.1"))

    def test_custom_and_github_manifests_are_supported(self):
        custom = parse_update_manifest(
            json.dumps(
                {
                    "version": "0.3.0",
                    "download_url": "https://example.test/SOOPTimeline.exe",
                    "release_notes": "주제 묶음 개선",
                }
            ),
            "0.2.0",
        )
        github = parse_update_manifest(
            {
                "tag_name": "v0.4.0",
                "html_url": "https://github.com/example/project/releases/tag/v0.4.0",
                "body": "새 버전",
            },
            "0.2.0",
        )

        self.assertTrue(custom.update_available)
        self.assertEqual(custom.latest_version, "0.3.0")
        self.assertTrue(github.update_available)
        self.assertEqual(github.release_notes, "새 버전")

    def test_update_preferences_come_from_database_or_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "test.db")
            try:
                database.set_setting(
                    UPDATE_MANIFEST_SETTING,
                    "https://example.test/update.json",
                )
                self.assertEqual(
                    configured_manifest_url(database),
                    "https://example.test/update.json",
                )
                database.set_setting(AUTO_UPDATE_CHECK_SETTING, "0")
                self.assertFalse(automatic_update_check_enabled(database))
                with patch.dict(
                    os.environ,
                    {
                        "SOOP_TIMELINE_UPDATE_MANIFEST_URL":
                            "https://env.test/update.json"
                    },
                ):
                    self.assertEqual(
                        configured_manifest_url(database),
                        "https://env.test/update.json",
                    )
            finally:
                database.close()

    def test_packaged_channel_can_supply_default_manifest_url(self):
        with tempfile.TemporaryDirectory() as directory:
            channel_path = Path(directory) / "update-channel.json"
            channel_path.write_text(
                json.dumps({"manifest_url": "https://bundle.test/update.json"}),
                encoding="utf-8",
            )
            with patch.object(sys, "_MEIPASS", directory, create=True):
                self.assertEqual(
                    bundled_manifest_url(),
                    "https://bundle.test/update.json",
                )


if __name__ == "__main__":
    unittest.main()
