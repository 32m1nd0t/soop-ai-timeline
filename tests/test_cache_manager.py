from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from soop_timeline.services.cache_manager import (
    cache_size_bytes,
    cleanup_expired_caches,
    has_vod_cache,
    remove_all_caches,
    remove_vod_cache,
    vod_cache_dir,
)


class CacheManagerTests(unittest.TestCase):
    def test_cache_operations_stay_inside_analysis_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch(
                "soop_timeline.services.cache_manager.analysis_data_dir",
                return_value=root,
            ):
                target = vod_cache_dir("../vod-123")
                self.assertEqual(target.parent, root.resolve())
                target.mkdir()
                (target / "transcript.json").write_bytes(b"12345")
                self.assertTrue(has_vod_cache("../vod-123"))
                self.assertEqual(cache_size_bytes(), 5)
                self.assertTrue(remove_vod_cache("../vod-123"))
                self.assertFalse(target.exists())

    def test_expired_and_all_cache_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old"
            recent = root / "recent"
            old.mkdir()
            recent.mkdir()
            old_time = (datetime.now(timezone.utc) - timedelta(days=40)).timestamp()
            os.utime(old, (old_time, old_time))
            with patch(
                "soop_timeline.services.cache_manager.analysis_data_dir",
                return_value=root,
            ):
                self.assertEqual(cleanup_expired_caches(30), 1)
                self.assertFalse(old.exists())
                self.assertTrue(recent.exists())
                self.assertEqual(remove_all_caches(), 1)
                self.assertFalse(recent.exists())


if __name__ == "__main__":
    unittest.main()
