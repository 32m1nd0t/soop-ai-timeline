"""A WebView2 window that posts a reviewed timeline into the user's SOOP session.

The window reuses the same persistent ``webview2`` profile as the review player,
so a sign-in done here (or there) is shared. SOOP's own login page handles the
password; this app never reads or stores it. Posting drives the same comment box
a signed-in user would use, one block at a time, only after an explicit confirm.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..models import Vod
from ..paths import app_data_dir
from ..services.comment_publisher import (
    PublicationPlan,
    build_comment_dump_script,
    build_login_probe_script,
    build_post_reply_script,
    build_post_root_script,
    build_verify_root_script,
    root_needle,
    vod_page_url,
)
from .review_player import ResilientQtWebView2Widget, build_close_script


logger = logging.getLogger(__name__)

_SOOP_HOME = "https://www.sooplive.com/"
# Delay after a submit click before we look for the result. SOOP posts the
# comment over XHR and re-renders, so give it time before verifying/replying.
_SETTLE_MS = 1800
_VERIFY_RETRY_MS = 1500
_MAX_VERIFY_ATTEMPTS = 3


class SoopCommentPublisher(QFrame):
    """Top-level window that logs in (via SOOP) and posts comment + replies."""

    closed = Signal()
    status_changed = Signal(str)

    def __init__(self, vod: Vod, blocks: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.vod = vod
        self._blocks = [block for block in blocks if block.strip()]
        self.setObjectName("playerCard")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setWindowTitle(f"SOOP 댓글 등록 · {vod.title}")
        self.resize(820, 720)
        self.setMinimumSize(560, 480)

        self._loaded = False
        self._logged_in = False
        self._busy = False
        self._replies: list[str] = []
        self._needle = ""
        self._posted_count = 0
        self._verify_attempts = 0
        # Auto-drive: log in if needed, then post after a single confirm.
        self._auto_posted = False
        self._login_redirect_done = False
        self._login_poll_timer = QTimer(self)
        self._login_poll_timer.setInterval(2500)
        self._login_poll_timer.timeout.connect(self._poll_login)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("SOOP 댓글·대댓글 등록")
        title.setObjectName("sectionTitle")
        self.reload_button = QPushButton("새로고침")
        self.reload_button.clicked.connect(self.reload_vod_page)
        self.login_button = QPushButton("SOOP 로그인 열기")
        self.login_button.clicked.connect(self.open_login_page)
        self.return_button = QPushButton("VOD 페이지로")
        self.return_button.clicked.connect(self.reload_vod_page)
        external_button = QPushButton("외부로 열기")
        external_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(self.vod.url))
        )
        close_button = QPushButton("닫기")
        close_button.clicked.connect(self.close)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.reload_button)
        header.addWidget(self.login_button)
        header.addWidget(self.return_button)
        header.addWidget(external_button)
        header.addWidget(close_button)
        layout.addLayout(header)

        self.status_label = QLabel("VOD 페이지를 불러오는 중…")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self._browser_data = app_data_dir() / "webview2"
        self._browser_data.mkdir(parents=True, exist_ok=True)
        self._player_layout = layout
        self.web_view = self._create_web_view()
        layout.addWidget(self.web_view, 1)

        action_row = QHBoxLayout()
        self.dump_button = QPushButton("댓글 영역 구조 저장(진단)")
        self.dump_button.setToolTip(
            "로그인된 댓글창의 HTML 구조를 로그 폴더에 저장합니다. "
            "자동 등록이 셀렉터를 못 찾을 때 이 파일로 정확한 위치를 맞춥니다."
        )
        self.dump_button.clicked.connect(self.dump_comment_dom)
        self.publish_button = QPushButton("댓글·대댓글 등록")
        self.publish_button.setObjectName("primaryButton")
        self.publish_button.setEnabled(False)
        self.publish_button.clicked.connect(self.start_publish)
        action_row.addWidget(self.dump_button)
        action_row.addStretch(1)
        action_row.addWidget(self.publish_button)
        layout.addLayout(action_row)

        self.help_label = QLabel(
            "로그인돼 있으면 확인창을 거쳐 자동으로 등록합니다. 로그인이 안 돼 있으면 "
            "로그인 페이지를 열어 드리고, 로그인하면 자동으로 이어집니다. "
            "비밀번호는 SOOP 로그인 페이지에서만 입력하며 앱은 저장하지 않습니다. "
            "등록된 댓글은 SOOP에서 직접 삭제해야 합니다."
        )
        self.help_label.setObjectName("muted")
        self.help_label.setWordWrap(True)
        layout.addWidget(self.help_label)

        self._update_block_summary()

    # -- WebView2 lifecycle --------------------------------------------------
    def _create_web_view(self) -> ResilientQtWebView2Widget:
        web_view = ResilientQtWebView2Widget(
            debug=False,
            context_menus=True,
            background_color="#ffffff",
            handle_new_window=True,
            lazyload=True,
            user_data_folder=str(self._browser_data),
            fullscreen_support=False,
            parent=self,
        )
        web_view.setMinimumHeight(300)
        web_view.bridge.domContentLoaded.connect(
            lambda candidate=web_view: (
                self._on_dom_loaded() if candidate is self.web_view else None
            )
        )
        web_view.native_control_failed.connect(
            lambda message: self.status_changed.emit(message)
        )
        return web_view

    def open_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        if not self._loaded:
            self._loaded = True
            self.web_view.load_url(vod_page_url(self.vod.vod_id))

    def reload_vod_page(self) -> None:
        self._set_status("VOD 페이지를 다시 불러옵니다…")
        self.web_view.load_url(vod_page_url(self.vod.vod_id))

    def open_login_page(self) -> None:
        self._login_redirect_done = True
        self._set_status(
            "SOOP 로그인 페이지를 엽니다. 로그인하면 자동으로 이어집니다."
        )
        self.web_view.load_url(_SOOP_HOME)
        self._start_login_poll()

    def _on_dom_loaded(self) -> None:
        if self._busy:
            return
        self.web_view.evaluate_js(build_login_probe_script(), self._on_login_probe)

    def _on_login_probe(self, result: dict) -> None:
        payload = self._payload(result)
        if payload is None:
            self._set_status("페이지 상태를 읽지 못했습니다. 새로고침 후 다시 시도하세요.")
            return
        url = str(payload.get("url", ""))
        on_vod_page = "/player/" in url
        has_input = bool(payload.get("hasCommentInput"))
        logged_in = bool(payload.get("loggedIn"))
        login_id = str(payload.get("loginId", ""))
        self._logged_in = logged_in
        ready = logged_in and on_vod_page and has_input
        self.publish_button.setEnabled(ready and bool(self._blocks) and not self._busy)

        if self._busy:
            return

        if ready:
            # Logged in and the box is present: stop waiting and drive posting.
            self._stop_login_poll()
            who = f" · 아이디 {login_id}" if login_id else ""
            if self._auto_posted:
                self._set_status(f"로그인 확인됨{who}.")
            else:
                self._auto_posted = True
                self._set_status(f"로그인 확인됨{who}. 등록을 시작합니다…")
                QTimer.singleShot(400, self.start_publish)
            return

        if logged_in and not on_vod_page:
            # Login finished on the SOOP home/login page; return to the VOD page
            # and the next probe will auto-post.
            self._stop_login_poll()
            self._set_status("로그인 확인됨. VOD 페이지로 이동합니다…")
            self.web_view.load_url(vod_page_url(self.vod.vod_id))
            return

        if logged_in and on_vod_page and not has_input:
            # Comment area may still be loading; keep re-probing briefly.
            self._set_status(
                f"로그인 확인됨({login_id}). 댓글 영역을 불러오는 중…"
            )
            self._start_login_poll()
            return

        # Not logged in: open the login page once, then poll until login lands.
        if not self._login_redirect_done:
            self._login_redirect_done = True
            self._set_status(
                "로그인이 필요합니다. 로그인 페이지를 엽니다. "
                "로그인하면 자동으로 VOD로 돌아와 등록을 이어갑니다."
            )
            self.web_view.load_url(_SOOP_HOME)
            self._start_login_poll()
        else:
            self._set_status("로그인을 기다리는 중… 로그인하면 자동으로 이어집니다.")

    def _start_login_poll(self) -> None:
        if not self._login_poll_timer.isActive():
            self._login_poll_timer.start()

    def _stop_login_poll(self) -> None:
        if self._login_poll_timer.isActive():
            self._login_poll_timer.stop()

    def _poll_login(self) -> None:
        if self._busy:
            return
        self.web_view.evaluate_js(build_login_probe_script(), self._on_login_probe)

    # -- Diagnostics ---------------------------------------------------------
    def dump_comment_dom(self) -> None:
        self.web_view.evaluate_js(build_comment_dump_script(), self._on_dump_ready)

    def _on_dump_ready(self, result: dict) -> None:
        payload = self._payload(result)
        if payload is None:
            QMessageBox.warning(self, "진단 실패", "페이지에서 구조를 읽지 못했습니다.")
            return
        html = str(payload.get("html", ""))
        log_dir = app_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        target = log_dir / "comment-dom-dump.html"
        try:
            target.write_text(html, encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, "진단 실패", f"파일 저장에 실패했습니다: {error}")
            return
        comment_count = payload.get("commentCount", 0)
        truncated = " (일부 잘림)" if payload.get("truncated") else ""
        QMessageBox.information(
            self,
            "댓글 영역 구조 저장",
            f"현재 댓글 {comment_count}개가 보입니다.{truncated}\n\n"
            f"저장 위치:\n{target}\n\n"
            "이 파일을 공유해 주시면 자동 등록 셀렉터를 정확히 맞출 수 있습니다.",
        )

    # -- Publishing ----------------------------------------------------------
    def start_publish(self) -> None:
        if self._busy:
            return
        if not self._blocks:
            QMessageBox.information(self, "등록할 내용 없음", "등록할 타임라인이 비어 있습니다.")
            return
        try:
            plan = PublicationPlan.from_blocks(self._blocks)
        except ValueError as error:
            QMessageBox.information(self, "등록할 내용 없음", str(error))
            return

        reply_count = len(plan.replies)
        confirm = QMessageBox.question(
            self,
            "SOOP에 실제 등록",
            f"'{self.vod.title}' 영상에 댓글 1개"
            + (f"와 대댓글 {reply_count}개" if reply_count else "")
            + "를 지금 실제로 등록합니다.\n\n"
            "등록된 댓글은 SOOP에서 직접 삭제해야 지워집니다. 계속할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self._busy = True
        self._stop_login_poll()
        self._replies = list(plan.replies)
        self._needle = root_needle(plan.root_comment)
        self._posted_count = 0
        self._verify_attempts = 0
        self.publish_button.setEnabled(False)
        self.dump_button.setEnabled(False)
        self._set_status("댓글(1/1)을 등록하는 중…")
        self.web_view.evaluate_js(
            build_post_root_script(plan.root_comment), self._on_root_result
        )

    def _on_root_result(self, result: dict) -> None:
        payload = self._payload(result)
        if payload is None or not payload.get("ok"):
            self._publish_failed("댓글", payload)
            return
        self._set_status("댓글 등록 요청 완료. 반영을 확인하는 중…")
        QTimer.singleShot(_SETTLE_MS, self._verify_root)

    def _verify_root(self) -> None:
        self.web_view.evaluate_js(
            build_verify_root_script(self._needle), self._on_verify_result
        )

    def _on_verify_result(self, result: dict) -> None:
        payload = self._payload(result)
        found = bool(payload and payload.get("found"))
        if found:
            self._posted_count = 1
            if self._replies:
                self._set_status("댓글 등록 확인. 대댓글을 이어서 등록합니다…")
                QTimer.singleShot(_SETTLE_MS, self._post_next_reply)
            else:
                self._publish_done()
            return
        self._verify_attempts += 1
        if self._verify_attempts < _MAX_VERIFY_ATTEMPTS:
            QTimer.singleShot(_VERIFY_RETRY_MS, self._verify_root)
            return
        # The comment may still have posted but rendered in a form we cannot match.
        proceed = QMessageBox.question(
            self,
            "댓글 확인 실패",
            "등록한 댓글을 화면에서 확인하지 못했습니다. "
            "이미 올라갔을 수도 있습니다.\n\n"
            + ("그래도 대댓글을 이어서 등록할까요?" if self._replies else "창을 확인해 주세요."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if self._replies and proceed == QMessageBox.StandardButton.Yes:
            self._posted_count = 1
            QTimer.singleShot(0, self._post_next_reply)
        else:
            self._finish_busy()
            self._set_status(
                "댓글만 등록 시도했습니다. 화면에서 결과를 확인하세요. "
                "남은 대댓글은 편집 탭의 '이 블록 복사'로 직접 등록할 수 있습니다."
            )

    def _post_next_reply(self) -> None:
        if not self._replies:
            self._publish_done()
            return
        index = self._posted_count  # 1-based reply order equals current posted count
        text = self._replies.pop(0)
        self._set_status(f"대댓글 {index}/{len(self._blocks) - 1}을 등록하는 중…")
        self.web_view.evaluate_js(
            build_post_reply_script(text, self._needle), self._on_reply_result
        )

    def _on_reply_result(self, result: dict) -> None:
        payload = self._payload(result)
        if payload is None or not payload.get("ok"):
            self._publish_failed(f"대댓글 {self._posted_count}", payload)
            return
        self._posted_count += 1
        if self._replies:
            QTimer.singleShot(_SETTLE_MS, self._post_next_reply)
        else:
            self._publish_done()

    def _publish_done(self) -> None:
        self._finish_busy()
        replies_done = max(0, self._posted_count - 1)
        self._set_status(f"완료: 댓글 1개 + 대댓글 {replies_done}개 등록했습니다.")
        QMessageBox.information(
            self,
            "등록 완료",
            f"댓글 1개와 대댓글 {replies_done}개를 등록했습니다.\n"
            "SOOP 페이지에서 실제 반영을 확인해 주세요.",
        )

    def _publish_failed(self, label: str, payload: dict | None) -> None:
        self._finish_busy()
        stage = payload.get("stage") if isinstance(payload, dict) else None
        stage_hint = {
            "not-logged-in": "로그인이 풀렸습니다. 다시 로그인한 뒤 시도하세요.",
            "find-input": "댓글 입력창을 찾지 못했습니다.",
            "find-submit": "등록 버튼을 찾지 못했습니다(내용은 입력됨).",
            "find-parent": "방금 올린 댓글을 찾지 못했습니다.",
            "find-reply-input": "대댓글 입력창을 찾지 못했습니다.",
            "find-reply-submit": "대댓글 등록 버튼을 찾지 못했습니다(내용은 입력됨).",
        }.get(stage or "", "자동 등록에 필요한 요소를 찾지 못했습니다.")
        posted = ""
        if self._posted_count:
            posted = f"\n\n여기까지 {self._posted_count}개 항목은 등록 시도되었습니다."
        QMessageBox.warning(
            self,
            "자동 등록 중단",
            f"{label} 단계에서 멈췄습니다: {stage_hint}{posted}\n\n"
            "'댓글 영역 구조 저장(진단)'으로 구조를 저장해 공유해 주시면 "
            "셀렉터를 맞춰 드립니다. 그 전에는 편집 탭의 '이 블록 복사'로 직접 등록하세요.",
        )
        self._set_status(f"{label} 단계에서 중단됨: {stage_hint}")

    def _finish_busy(self) -> None:
        self._busy = False
        self.publish_button.setEnabled(self._logged_in and bool(self._blocks))
        self.dump_button.setEnabled(True)

    # -- Helpers -------------------------------------------------------------
    def _update_block_summary(self) -> None:
        total = len(self._blocks)
        replies = max(0, total - 1)
        self.publish_button.setText(
            f"댓글·대댓글 등록 (댓글 1 + 대댓글 {replies})" if total else "등록할 내용 없음"
        )

    def _set_status(self, message: str) -> None:
        self.status_label.setText(message)
        self.status_changed.emit(message)

    @staticmethod
    def _payload(result: object) -> dict | None:
        if not isinstance(result, dict) or not result.get("success"):
            return None
        payload = result.get("result")
        return payload if isinstance(payload, dict) else None

    def closeEvent(self, event) -> None:
        self._stop_login_poll()
        try:
            self.web_view.evaluate_js(build_close_script())
        except Exception:
            pass
        self.closed.emit()
        super().closeEvent(event)
