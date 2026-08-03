import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from soop_timeline.services.update_installer import download_verified_installer


class _FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, url: str = "https://example.test/setup.exe"):
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))}
        self._url = url

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class UpdateInstallerTests(unittest.TestCase):
    def test_verified_download_is_published_atomically(self):
        payload = b"verified installer payload"
        expected = hashlib.sha256(payload).hexdigest()
        progress: list[tuple[int, int]] = []
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "SOOPTimeline-Setup.exe"
            with patch(
                "soop_timeline.services.update_installer.urlopen",
                return_value=_FakeResponse(payload),
            ):
                result = download_verified_installer(
                    "https://example.test/setup.exe",
                    destination,
                    expected,
                    user_agent="test/1",
                    progress=lambda received, total: progress.append((received, total)),
                )

            self.assertEqual(result.read_bytes(), payload)
            self.assertFalse(destination.with_name(destination.name + ".part").exists())
            self.assertEqual(progress[-1], (len(payload), len(payload)))

    def test_digest_mismatch_keeps_existing_app_and_removes_partial_file(self):
        payload = b"tampered installer"
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "SOOPTimeline-Setup.exe"
            with patch(
                "soop_timeline.services.update_installer.urlopen",
                return_value=_FakeResponse(payload),
            ):
                with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                    download_verified_installer(
                        "https://example.test/setup.exe",
                        destination,
                        "00" * 32,
                        user_agent="test/1",
                    )

            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_name(destination.name + ".part").exists())

    def test_https_download_cannot_redirect_to_plain_http(self):
        payload = b"payload"
        expected = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "SOOPTimeline-Setup.exe"
            with patch(
                "soop_timeline.services.update_installer.urlopen",
                return_value=_FakeResponse(payload, "http://example.test/setup.exe"),
            ):
                with self.assertRaisesRegex(RuntimeError, "안전하지 않은"):
                    download_verified_installer(
                        "https://example.test/setup.exe",
                        destination,
                        expected,
                        user_agent="test/1",
                    )


if __name__ == "__main__":
    unittest.main()
