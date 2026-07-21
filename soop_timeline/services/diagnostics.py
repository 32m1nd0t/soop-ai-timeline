from __future__ import annotations

from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import sys
import traceback
import zipfile

from .. import __version__
from ..paths import app_data_dir, database_path
from .transcription import detect_whisper_runtime


LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "soop-timeline.log"


def log_file_path() -> Path:
    directory = app_data_dir() / LOG_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory / LOG_FILE_NAME


def configure_logging() -> Path:
    destination = log_file_path()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(
        isinstance(handler, RotatingFileHandler)
        and Path(getattr(handler, "baseFilename", "")) == destination
        for handler in root.handlers
    ):
        handler = RotatingFileHandler(
            destination,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(handler)
    logging.getLogger(__name__).info("Application start version=%s", __version__)
    return destination


def install_exception_hook() -> None:
    previous = sys.excepthook

    def handle(error_type: type[BaseException], error: BaseException, tb: object) -> None:
        logging.getLogger("soop_timeline.unhandled").critical(
            "Unhandled exception",
            exc_info=(error_type, error, tb),
        )
        previous(error_type, error, tb)

    sys.excepthook = handle


def build_diagnostic_report(database: object) -> str:
    model = database.get_setting("gemini_model", "gemini-2.5-flash-lite")
    whisper_model = database.get_setting("whisper_model", "large-v3-turbo")
    whisper_device = database.get_setting("whisper_device", "auto")
    try:
        runtime = detect_whisper_runtime(whisper_device)
        runtime_text = runtime.description
        runtime_warning = runtime.warning or "없음"
    except Exception as error:
        runtime_text = f"감지 실패: {error}"
        runtime_warning = "확인 불가"

    report = [
        "SOOP AI 타임라인 진단 정보",
        f"생성 시각: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"앱 버전: {__version__}",
        f"패키징 실행: {bool(getattr(sys, 'frozen', False))}",
        f"운영체제: {platform.platform()}",
        f"Python: {platform.python_version()} ({platform.architecture()[0]})",
        "AI 공급자: Google Gemini",
        f"Gemini 모델: {model}",
        "API 키: 포함하지 않음",
        f"Whisper 모델: {whisper_model}",
        f"Whisper 설정 장치: {whisper_device}",
        f"Whisper 감지: {runtime_text}",
        f"Whisper 경고: {runtime_warning}",
        f"데이터 폴더: {app_data_dir()}",
        f"DB 위치: {database_path()}",
        f"로그 위치: {log_file_path()}",
        "",
        "최근 로그 (API 키·자막 원문은 기록하지 않음)",
        "-" * 60,
        _recent_log_text(),
    ]
    return "\n".join(report).rstrip() + "\n"


def create_diagnostic_bundle(destination: str | Path, database: object) -> Path:
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    report = build_diagnostic_report(database)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnostics.txt", report)
        log_path = log_file_path()
        if log_path.is_file():
            archive.write(log_path, "logs/soop-timeline.log")
        for index in range(1, 4):
            rotated = log_path.with_name(f"{log_path.name}.{index}")
            if rotated.is_file():
                archive.write(rotated, f"logs/{rotated.name}")
    return target


def log_exception(context: str) -> None:
    logging.getLogger(__name__).error("%s\n%s", context, traceback.format_exc())


def _recent_log_text(max_chars: int = 60_000) -> str:
    try:
        text = log_file_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "로그 없음"
    return text[-max_chars:]
