from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ..paths import app_data_dir


MAX_INSTALLER_BYTES = 2 * 1024 * 1024 * 1024


class UpdateDownloadCancelled(RuntimeError):
    pass


def installer_download_path(version: str) -> Path:
    safe_version = re.sub(r"[^0-9A-Za-z._-]", "_", version).strip("._")
    update_dir = app_data_dir() / "updates"
    update_dir.mkdir(parents=True, exist_ok=True)
    return update_dir / f"SOOPTimeline-Setup-{safe_version or 'latest'}.exe"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_verified_installer(
    url: str,
    destination: Path,
    expected_sha256: str,
    *,
    user_agent: str,
    cancelled: Callable[[], bool] = lambda: False,
    progress: Callable[[int, int], None] = lambda _received, _total: None,
) -> Path:
    """Download to a partial file and publish it only after SHA-256 validation."""

    expected = expected_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("업데이트 설치 파일의 SHA-256 검증값이 올바르지 않습니다.")
    if urlparse(url).scheme.lower() != "https":
        raise ValueError("자동 업데이트 설치 파일은 HTTPS 주소에서만 받을 수 있습니다.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and hmac.compare_digest(file_sha256(destination), expected):
        progress(destination.stat().st_size, destination.stat().st_size)
        return destination

    partial = destination.with_name(destination.name + ".part")
    partial.unlink(missing_ok=True)
    request = Request(url, headers={"User-Agent": user_agent})
    digest = hashlib.sha256()
    received = 0
    try:
        with urlopen(request, timeout=30) as response:
            final_url = str(response.geturl() or "")
            if urlparse(final_url).scheme.lower() != "https":
                raise RuntimeError("업데이트 다운로드가 안전하지 않은 주소로 이동했습니다.")
            content_length = str(response.headers.get("Content-Length") or "").strip()
            total = int(content_length) if content_length.isdigit() else 0
            if total > MAX_INSTALLER_BYTES:
                raise RuntimeError("업데이트 설치 파일이 허용 크기를 초과합니다.")

            with partial.open("wb") as output:
                while True:
                    if cancelled():
                        raise UpdateDownloadCancelled("업데이트 다운로드를 취소했습니다.")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > MAX_INSTALLER_BYTES:
                        raise RuntimeError("업데이트 설치 파일이 허용 크기를 초과합니다.")
                    output.write(chunk)
                    digest.update(chunk)
                    progress(received, total)

        if not hmac.compare_digest(digest.hexdigest(), expected):
            raise RuntimeError(
                "다운로드한 업데이트의 SHA-256이 배포 정보와 일치하지 않습니다. "
                "파일을 실행하지 않았습니다."
            )
        partial.replace(destination)
        return destination
    except Exception:
        partial.unlink(missing_ok=True)
        raise
