import re
import unittest
from pathlib import Path

from soop_timeline import __version__


class VersionSyncTests(unittest.TestCase):
    def test_package_and_application_versions_match(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), __version__)


if __name__ == "__main__":
    unittest.main()
