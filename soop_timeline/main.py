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

    if "--smoke-test" in sys.argv:
        try:
            _verify_packaged_dependencies()
        except Exception:
            report_path = app_data_dir() / "package-smoke-error.txt"
            report_path.write_text(traceback.format_exc(), encoding="utf-8")
            return 2

    database = Database(database_path())
    app.aboutToQuit.connect(database.close)

    window = MainWindow(database)
    window.show()
    if startup_vod_id:
        QTimer.singleShot(0, lambda: window.open_timeline(startup_vod_id))
    if "--smoke-test" in sys.argv:
        QTimer.singleShot(800, app.quit)
    return app.exec()


def _verify_packaged_dependencies() -> None:
    import av  # noqa: F401
    import keyring
    from faster_whisper import BatchedInferencePipeline, WhisperModel  # noqa: F401
    from google import genai  # noqa: F401
    from google.genai import types
    keyring.get_keyring()
    types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema={
            "type": "object",
            "properties": {"ok": {"type": "string"}},
            "required": ["ok"],
        },
    )
