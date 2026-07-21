from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil

from ..paths import analysis_data_dir


def cache_root() -> Path:
    return analysis_data_dir().resolve()


def cache_size_bytes() -> int:
    root = cache_root()
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def human_size(value: int) -> str:
    size = max(0.0, float(value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def vod_cache_dir(vod_id: str) -> Path:
    safe_id = "".join(
        character for character in str(vod_id) if character.isalnum() or character in "-_"
    ) or "unknown"
    root = cache_root()
    target = (root / safe_id).resolve()
    if target.parent != root:
        raise ValueError("자막 캐시 경로가 올바르지 않습니다.")
    return target


def has_vod_cache(vod_id: str, filename: str | None = None) -> bool:
    target = vod_cache_dir(vod_id)
    if filename:
        return (target / filename).is_file()
    if not target.is_dir():
        return False
    try:
        return any(target.iterdir())
    except OSError:
        return False


def remove_vod_cache(vod_id: str) -> bool:
    target = vod_cache_dir(vod_id)
    if not target.is_dir():
        return False
    try:
        shutil.rmtree(target)
    except OSError:
        return False
    return True


def remove_all_caches() -> int:
    root = cache_root()
    removed = 0
    for child in list(root.iterdir()):
        resolved = child.resolve()
        if resolved.parent != root:
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child)
                removed += 1
            elif child.is_file():
                child.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def cleanup_expired_caches(retention_days: int) -> int:
    if retention_days <= 0:
        return 0
    root = cache_root()
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for child in list(root.iterdir()):
        try:
            modified = datetime.fromtimestamp(child.stat().st_mtime, timezone.utc)
        except OSError:
            continue
        if modified >= cutoff:
            continue
        resolved = child.resolve()
        if resolved.parent != root:
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child)
            elif child.is_file():
                child.unlink()
            else:
                continue
        except OSError:
            continue
        removed += 1
    return removed
