import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

from soop_timeline.database import Database
from soop_timeline.services.diagnostics import (
    build_diagnostic_report,
    create_diagnostic_bundle,
)


class DiagnosticsTests(unittest.TestCase):
    def test_report_and_bundle_exclude_secrets(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"SOOP_TIMELINE_DATA_DIR": directory},
        ):
            database = Database(Path(directory) / "timeline.db")
            database.set_setting("ai_provider", "openai")
            database.set_setting("ai_model_openai", "test-model")
            report = build_diagnostic_report(database)
            self.assertIn("AI 공급자: OpenAI", report)
            self.assertIn("API 키: 포함하지 않음", report)

            destination = Path(directory) / "diagnostics.zip"
            create_diagnostic_bundle(destination, database)
            with zipfile.ZipFile(destination) as archive:
                self.assertIn("diagnostics.txt", archive.namelist())
                bundled = archive.read("diagnostics.txt").decode("utf-8")
                self.assertNotIn("OPENAI_API_KEY", bundled)
            database.close()


if __name__ == "__main__":
    unittest.main()
