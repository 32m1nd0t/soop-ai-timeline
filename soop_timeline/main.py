from __future__ import annotations

import sys
import traceback


def option_value(arguments: list[str], name: str) -> str:
    try:
        index = arguments.index(name)
    except ValueError:
        return ""
    if index + 1 >= len(arguments):
        return ""
    return arguments[index + 1].strip()


def main() -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from .database import Database
    from .paths import app_data_dir, database_path
    from .services.diagnostics import configure_logging, install_exception_hook
    from .styles import APP_STYLE
    from .ui.main_window import MainWindow

    startup_vod_id = option_value(sys.argv, "--open-vod")
    configure_logging()
    install_exception_hook()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("SOOP AI 타임라인")
    app.setOrganizationName("SOOPTimeline")
    app.setStyle("Fusion")
    app.setFont(QFont("Malgun Gothic", 10))
    app.setStyleSheet(APP_STYLE)

    smoke_test = "--smoke-test" in sys.argv
    gpu_smoke_test = "--gpu-smoke-test" in sys.argv
    if smoke_test or gpu_smoke_test:
        try:
            _verify_packaged_dependencies()
            if gpu_smoke_test:
                _verify_gpu_runtime()
        except Exception:
            report_path = app_data_dir() / "package-smoke-error.txt"
            report_path.write_text(traceback.format_exc(), encoding="utf-8")
            return 2
        if gpu_smoke_test:
            (app_data_dir() / "gpu-smoke-ok.txt").write_text(
                "CUDA 12 cuBLAS/cuDNN 9 and faster-whisper runtime detected.\n",
                encoding="utf-8",
            )
            return 0

    database = Database(database_path())
    if smoke_test:
        from .services.preferences import PRIVACY_NOTICE_SETTING, PRIVACY_NOTICE_VERSION

        database.set_setting(PRIVACY_NOTICE_SETTING, PRIVACY_NOTICE_VERSION)
    app.aboutToQuit.connect(database.close)

    window = MainWindow(database)
    window.show()
    if startup_vod_id:
        QTimer.singleShot(0, lambda: window.open_timeline(startup_vod_id))
    if smoke_test:
        QTimer.singleShot(800, app.quit)
    return app.exec()


def _verify_packaged_dependencies() -> None:
    import importlib.util

    import av  # noqa: F401
    import keyring
    from faster_whisper import BatchedInferencePipeline, WhisperModel  # noqa: F401
    from google import genai  # noqa: F401
    from google.genai import types
    from .services.transcription import configure_nvidia_runtime_paths

    for package in ("nvidia.cublas", "nvidia.cudnn"):
        if importlib.util.find_spec(package) is None:
            raise RuntimeError(f"패키지에 {package} CUDA 런타임이 포함되지 않았습니다.")
    configure_nvidia_runtime_paths()
    keyring.get_keyring()
    types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema={
            "type": "object",
            "properties": {"ok": {"type": "string"}},
            "required": ["ok"],
        },
    )


def _verify_gpu_runtime() -> None:
    from .services.transcription import detect_whisper_runtime

    runtime = detect_whisper_runtime("cuda")
    if runtime.device != "cuda":
        raise RuntimeError("CUDA GPU 런타임 확인에 실패했습니다.")
