from __future__ import annotations

import os
from pathlib import Path


APP_DIR_NAME = "SOOPTimeline"


def app_data_dir() -> Path:
    override = os.environ.get("SOOP_TIMELINE_DATA_DIR")
    if override:
        path = Path(override).expanduser().resolve()
    elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        path = Path(os.environ["LOCALAPPDATA"]) / APP_DIR_NAME
    else:
        path = Path.home() / ".local" / "share" / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    return app_data_dir() / "timeline.db"


def analysis_data_dir(vod_id: str | None = None) -> Path:
    path = app_data_dir() / "analysis"
    if vod_id:
        safe_vod_id = "".join(character for character in vod_id if character.isalnum() or character in "-_")
        path /= safe_vod_id or "unknown"
    path.mkdir(parents=True, exist_ok=True)
    return path
