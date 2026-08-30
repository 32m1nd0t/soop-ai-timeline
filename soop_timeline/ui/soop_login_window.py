from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..paths import app_data_dir
from ..services.soop_auth import (
    clear_soop_session_cookies,
    store_soop_session_cookies,
)
from .review_player import ResilientQtWebView2Widget


_SOOP_LOGIN_URL = "https://login.sooplive.com/afreeca/login.php"


def _remove_ephemeral_profile(path: Path, attempt: int = 0) -> None:
    """Best-effort removal of one isolated adult-login browser profile."""

    auth_root = (app_data_dir() / "auth_webview2").resolve()
    try:
        target = path.resolve()
    except OSError:
        return
    if target.parent != auth_root:
        return
    try:
        shutil.rmtree(target)
    except FileNotFoundError:
        return
    except OSError:
        if attempt < 8:
            QTimer.singleShot(
                500,
                lambda target=target, retry=attempt + 1:
                _remove_ephemeral_profile(target, retry),
            )


def cleanup_ephemeral_soop_login_profiles() -> None:
    """Remove adult-login profiles left behind by an interrupted app exit."""

    auth_root = app_data_dir() / "auth_webview2"
    if not auth_root.exists():
        return
    try:
        candidates = tuple(auth_root.iterdir())
    except OSError:
        return
    for candidate in candidates:
        if candidate.is_dir():
            _remove_ephemeral_profile(candidate)


class SoopLoginWindow(QFrame):
    """Collect an adult-verified SOOP session without handling credentials."""

    authenticated = Signal()
    closed = Signal()

    def __init__(
        self,
        target_url: str,
        description: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.target_url = str(target_url or "https://www.sooplive.com/")
        self.description = str(description or "19세 SOOP 콘텐츠")
        self._completed = False
        self._busy = False

        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setWindowTitle("SOOP 로그인 · 19세 콘텐츠 분석")
        self.resize(880, 760)
        self.setMinimumSize(600, 500)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("SOOP 로그인 및 성인 인증")
        title.setObjectName("sectionTitle")
        login_button = QPushButton("로그인 페이지")
        login_button.clicked.connect(
            lambda: self.web_view.load_url(_SOOP_LOGIN_URL)
        )
        target_button = QPushButton("19세 영상으로 돌아가기")
        target_button.clicked.connect(
            lambda: self.web_view.load_url(self.target_url)
        )
        close_button = QPushButton("취소")
        close_button.clicked.connect(self.close)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(login_button)
        header.addWidget(target_button)
        header.addWidget(close_button)
        layout.addLayout(header)

        self.status_label = QLabel(
            f"{self.description}에 접근하려면 이 창에서 SOOP에 로그인하고 "
            "성인 인증/시청 확인을 완료하세요."
        )
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        browser_root = app_data_dir() / "auth_webview2"
        browser_root.mkdir(parents=True, exist_ok=True)
        self._browser_data = browser_root / uuid4().hex
        self._browser_data.mkdir(parents=True, exist_ok=True)
        self.web_view = ResilientQtWebView2Widget(
            debug=False,
            context_menus=True,
            background_color="#ffffff",
            handle_new_window=True,
            lazyload=True,
            user_data_folder=str(self._browser_data),
            fullscreen_support=False,
            parent=self,
        )
        self.web_view.setMinimumHeight(360)
        self.web_view.bridge.domContentLoaded.connect(self._on_dom_loaded)
        self.web_view.native_control_failed.connect(self._on_webview_failed)
        layout.addWidget(self.web_view, 1)

        action_row = QHBoxLayout()
        help_label = QLabel(
            "비밀번호는 SOOP 페이지에서만 입력되며 앱은 읽거나 저장하지 않습니다. "
            "이 로그인 창은 임시 브라우저 프로필을 쓰며, 완료 후 쿠키를 지웁니다. "
            "해당 19세 작업에 필요한 세션만 메모리로 가져옵니다."
        )
        help_label.setObjectName("muted")
        help_label.setWordWrap(True)
        self.complete_button = QPushButton("로그인·성인 인증 완료")
        self.complete_button.setObjectName("primaryButton")
        self.complete_button.clicked.connect(self._complete_login)
        action_row.addWidget(help_label, 1)
        action_row.addWidget(self.complete_button)
        layout.addLayout(action_row)

    def open_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.web_view.load_url(self.target_url)

    def _on_dom_loaded(self) -> None:
        if not self._busy:
            self.status_label.setText(
                "페이지가 열렸습니다. 로그인과 성인 인증/시청 확인을 마친 뒤 "
                "아래 완료 버튼을 누르세요."
            )

    def _complete_login(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.complete_button.setEnabled(False)
        self.status_label.setText("SOOP 로그인 세션을 확인하는 중…")
        self.web_view.get_soop_cookies(self._on_cookies_ready)

    def _on_cookies_ready(self, cookies: list[object], error: str) -> None:
        self._busy = False
        self.complete_button.setEnabled(True)
        if error:
            self.status_label.setText(error)
            return
        normalized = [item for item in cookies if isinstance(item, dict)]
        count = store_soop_session_cookies(normalized)
        if count <= 0:
            self.status_label.setText(
                "SOOP 로그인 세션을 찾지 못했습니다. 이 창에서 로그인한 뒤 "
                "다시 완료 버튼을 누르세요."
            )
            return
        if not self.web_view.clear_browser_cookies():
            clear_soop_session_cookies()
            self.status_label.setText(
                "임시 로그인 정보를 안전하게 지우지 못했습니다. 로그인 창을 닫고 "
                "다시 시도하세요."
            )
            return
        self._completed = True
        self.status_label.setText("로그인 세션 확인 완료 · 원래 작업을 다시 시작합니다…")
        self.authenticated.emit()
        self.close()

    def _on_webview_failed(self, message: str) -> None:
        self.status_label.setText(f"로그인 창 오류: {message}")

    def closeEvent(self, event) -> None:
        try:
            self.web_view.close()
        except Exception:
            pass
        QTimer.singleShot(
            250,
            lambda path=self._browser_data: _remove_ephemeral_profile(path),
        )
        if not self._completed:
            self.closed.emit()
        event.accept()
