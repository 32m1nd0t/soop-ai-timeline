from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse


UPDATE_MANIFEST_SETTING = "update_manifest_url"
AUTO_UPDATE_CHECK_SETTING = "auto_check_updates"
UPDATE_URL_ENVIRONMENT = "SOOP_TIMELINE_UPDATE_MANIFEST_URL"
DEFAULT_UPDATE_MANIFEST_URL = (
    "https://api.github.com/repos/32m1nd0t/soop-ai-timeline/releases/latest"
)


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    download_url: str
    release_notes: str = ""
    published_at: str = ""

    @property
    def update_available(self) -> bool:
        return is_newer_version(self.latest_version, self.current_version)


def configured_manifest_url(database: object) -> str:
    environment_url = (
        os.environ.get(UPDATE_URL_ENVIRONMENT, "")
        or os.environ.get("SOOP_TIMELINE_UPDATE_URL", "")
    ).strip()
    if environment_url:
        return environment_url
    saved_url = str(database.get_setting(UPDATE_MANIFEST_SETTING, "") or "").strip()
    return saved_url or bundled_manifest_url() or DEFAULT_UPDATE_MANIFEST_URL


def bundled_manifest_url() -> str:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    channel_path = bundle_root / "update-channel.json"
    if not channel_path.is_file():
        return ""
    try:
        payload = json.loads(channel_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("manifest_url") or "").strip()


def automatic_update_check_enabled(database: object) -> bool:
    value = str(database.get_setting(AUTO_UPDATE_CHECK_SETTING, "1") or "1")
    return value.strip().lower() not in {"0", "false", "no", "off"}


def parse_update_manifest(
    payload: bytes | str | Mapping[str, object],
    current_version: str,
) -> UpdateInfo:
    if isinstance(payload, bytes):
        if len(payload) > 1_000_000:
            raise ValueError("업데이트 정보가 허용 크기를 초과했습니다.")
        payload = payload.decode("utf-8-sig")
    if isinstance(payload, str):
        raw = json.loads(payload)
    else:
        raw = dict(payload)
    if not isinstance(raw, dict):
        raise ValueError("업데이트 정보 형식이 올바르지 않습니다.")

    # 자체 update.json과 GitHub의 releases/latest 응답을 모두 지원합니다.
    latest_version = str(raw.get("version") or raw.get("tag_name") or "").strip()
    latest_version = latest_version.removeprefix("v").removeprefix("V")
    if not latest_version or _version_key(latest_version) is None:
        raise ValueError("업데이트 정보에 올바른 버전이 없습니다.")

    download_url = str(raw.get("download_url") or raw.get("html_url") or "").strip()
    if not download_url:
        assets = raw.get("assets")
        if isinstance(assets, list):
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                candidate = str(asset.get("browser_download_url") or "").strip()
                name = str(asset.get("name") or "").lower()
                if candidate and (name.endswith(".exe") or not download_url):
                    download_url = candidate
                    if name.endswith(".exe"):
                        break

    if download_url:
        scheme = urlparse(download_url).scheme.lower()
        if scheme not in {"https", "http"}:
            download_url = ""

    release_notes = str(
        raw.get("release_notes") or raw.get("notes") or raw.get("body") or ""
    ).strip()
    published_at = str(raw.get("published_at") or "").strip()
    return UpdateInfo(
        current_version=current_version,
        latest_version=latest_version,
        download_url=download_url,
        release_notes=release_notes,
        published_at=published_at,
    )


def is_newer_version(candidate: str, current: str) -> bool:
    candidate_key = _version_key(candidate)
    current_key = _version_key(current)
    if candidate_key is None or current_key is None:
        return False
    return candidate_key > current_key


def _version_key(
    value: str,
) -> tuple[tuple[int, ...], int, tuple[tuple[int, object], ...]] | None:
    normalized = value.strip().lstrip("vV")
    match = re.fullmatch(
        r"(?P<release>\d+(?:\.\d+){0,3})"
        r"(?:-(?P<label>[0-9A-Za-z.-]+))?"
        r"(?:\+[0-9A-Za-z.-]+)?",
        normalized,
    )
    if match is None:
        return None

    release = tuple(int(part) for part in match.group("release").split("."))
    release += (0,) * (4 - len(release))
    label = match.group("label")
    if label is None:
        return release, 1, ()

    prerelease: list[tuple[int, object]] = []
    for part in re.split(r"[.-]", label.lower()):
        prerelease.append((0, int(part)) if part.isdigit() else (1, part))
    return release, 0, tuple(prerelease)
