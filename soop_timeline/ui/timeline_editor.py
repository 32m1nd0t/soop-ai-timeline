from __future__ import annotations

from PySide6.QtCore import QTimer, Qt, QUrl, Signal
from PySide6.QtGui import (
    QDesktopServices,
    QKeySequence,
    QMouseEvent,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..models import Vod
from ..services.text_editing import find_literal_matches, replace_literal_all
from ..services.timeline_blocks import COMMENT_LIMIT, block_label, split_timeline
from ..services.timeline_timestamp import timestamp_at_position
from .review_player import SoopReviewPlayer


class TimelineTextEdit(QPlainTextEdit):
    timestamp_activated = Signal(int)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        cursor = self.cursorForPosition(event.position().toPoint())
        block = cursor.block()
        position_in_block = cursor.position() - block.position()
        hit = timestamp_at_position(block.text(), position_in_block)
        if hit is None:
            super().mouseDoubleClickEvent(event)
            return

        cursor.setPosition(block.position() + hit.start)
        cursor.setPosition(
            block.position() + hit.end,
            QTextCursor.MoveMode.KeepAnchor,
        )
        self.setTextCursor(cursor)
        self.timestamp_activated.emit(hit.seconds)
        event.accept()


class TimelineBlockWidget(QFrame):
    text_changed = Signal()
    copy_requested = Signal(int)
    timestamp_activated = Signal(int)

    def __init__(self, index: int, total: int, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.index = index
        self.setObjectName("blockCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(9)

        header = QHBoxLayout()
        self.name_label = QLabel(block_label(index, total))
        self.name_label.setObjectName("sectionTitle")
        self.count_label = QLabel()
        self.count_label.setObjectName("muted")
        copy_button = QPushButton("이 블록 복사")
        copy_button.clicked.connect(lambda: self.copy_requested.emit(self.index))
        header.addWidget(self.name_label)
        header.addStretch(1)
        header.addWidget(self.count_label)
        header.addWidget(copy_button)

        self.editor = TimelineTextEdit(text)
        self.editor.setPlaceholderText("00:00:00 타임라인 내용을 입력하세요")
        self.editor.setToolTip(
            "타임스탬프를 더블클릭하면 SOOP 검수 플레이어가 해당 지점으로 이동합니다."
        )
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.editor.setMinimumHeight(190)
        self.editor.setTabChangesFocus(True)
        cursor = self.editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.editor.setTextCursor(cursor)
        self.editor.textChanged.connect(self._on_text_changed)
        self.editor.timestamp_activated.connect(self.timestamp_activated)

        layout.addLayout(header)
        layout.addWidget(self.editor)
        self.update_count()

    def text(self) -> str:
        return self.editor.toPlainText()

    def set_total(self, total: int) -> None:
        self.name_label.setText(block_label(self.index, total))

    def update_count(self) -> None:
        length = len(self.text())
        self.count_label.setText(f"{length:,} / {COMMENT_LIMIT:,}자")
        color = "#b42318" if length > COMMENT_LIMIT else "#687386"
        self.count_label.setStyleSheet(f"color: {color};")

    def _on_text_changed(self) -> None:
        self.update_count()
        self.text_changed.emit()


class TimelineDocumentEditor(QWidget):
    document_changed = Signal(str, str)
    review_completed = Signal(str)
    analysis_requested = Signal(str)
    analysis_cancel_requested = Signal(str)
    style_requested = Signal(str)

    def __init__(
        self,
        vod: Vod,
        text: str,
        analyzer_available: bool,
        analyzer_unavailable_reason: str = "",
        style_available: bool = False,
        style_unavailable_reason: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.vod = vod
        self._blocks: list[TimelineBlockWidget] = []
        self._rebuilding = False
        self._analyzer_available = analyzer_available
        self._style_available = style_available
        self._is_live = vod.source_kind == "live"
        self._search_matches: list[tuple[int, int]] = []
        self._search_index = -1
        self._find_replace_undo_text: str | None = None

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._emit_document_changed)

        self._rebalance_timer = QTimer(self)
        self._rebalance_timer.setSingleShot(True)
        self._rebalance_timer.setInterval(450)
        self._rebalance_timer.timeout.connect(self._rebalance_blocks)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        card = QFrame()
        card.setObjectName("editorCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)

        title_row = QHBoxLayout()
        title = QLabel(vod.title)
        title.setObjectName("sectionTitle")
        title.setWordWrap(True)
        open_button = QPushButton(
            "SOOP 라이브 열기" if self._is_live else "SOOP에서 열기"
        )
        open_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(self.vod.url))
        )
        self.player_button = QPushButton("검수 플레이어 열기")
        self.player_button.clicked.connect(self.toggle_review_player)
        self.player_button.setVisible(not self._is_live)
        title_row.addWidget(title, 1)
        title_row.addWidget(self.player_button)
        title_row.addWidget(open_button)

        meta = QLabel(
            f"{vod.streamer_name}  ·  {vod.published_text or '업로드 시각 미확인'}"
            f"  ·  {vod.duration_text or '영상 길이 미확인'}"
        )
        meta.setObjectName("muted")
        card_layout.addLayout(title_row)
        card_layout.addWidget(meta)
        root.addWidget(card)

        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setObjectName("notice")
        root.addWidget(self.notice)

        summary_row = QHBoxLayout()
        self.total_label = QLabel()
        self.total_label.setObjectName("statusText")
        self.analyze_button = QPushButton("SOOP 영상 AI 분석")
        self.analyze_button.setObjectName("primaryButton")
        self.analyze_button.clicked.connect(
            lambda: self.analysis_requested.emit(self.vod.vod_id)
        )
        self.cancel_analysis_button = QPushButton("분석 취소")
        self.cancel_analysis_button.setVisible(False)
        if self._is_live:
            self.cancel_analysis_button.setText("라이브 종료 및 정리")
        self.cancel_analysis_button.clicked.connect(
            lambda: self.analysis_cancel_requested.emit(self.vod.vod_id)
        )
        self.style_button = QPushButton("Gemini 문체 교정")
        self.style_button.setToolTip(
            "Whisper 재분석 없이 현재 타임라인만 건조한 제목형으로 교정합니다."
        )
        self.style_button.clicked.connect(
            lambda: self.style_requested.emit(self.vod.vod_id)
        )
        self.find_button = QPushButton("찾기·바꾸기")
        self.find_button.setToolTip("전체 댓글 블록에서 단어를 찾거나 일괄 변경합니다. (Ctrl+F)")
        self.find_button.clicked.connect(self.toggle_find_replace)
        self.copy_all_button = QPushButton("전체 텍스트 복사")
        self.copy_all_button.clicked.connect(self.copy_all)
        self.ready_button = QPushButton("검수 완료")
        self.ready_button.setObjectName("primaryButton")
        self.ready_button.clicked.connect(self.mark_review_complete)
        self.publish_button = QPushButton("SOOP에 작성")
        self.publish_button.setEnabled(False)
        self.publish_button.setToolTip("공식 댓글 API 권한을 받은 뒤 활성화됩니다.")
        summary_row.addWidget(self.total_label)
        summary_row.addStretch(1)
        summary_row.addWidget(self.analyze_button)
        summary_row.addWidget(self.cancel_analysis_button)
        summary_row.addWidget(self.style_button)
        summary_row.addWidget(self.find_button)
        summary_row.addWidget(self.copy_all_button)
        summary_row.addWidget(self.ready_button)
        summary_row.addWidget(self.publish_button)
        root.addLayout(summary_row)

        self.find_replace_bar = self._build_find_replace_bar()
        self.find_replace_bar.setVisible(False)
        root.addWidget(self.find_replace_bar)

        self.find_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        self.find_shortcut.activated.connect(self.show_find_replace)
        self.replace_shortcut = QShortcut(QKeySequence("Ctrl+H"), self)
        self.replace_shortcut.activated.connect(
            lambda: self.show_find_replace(focus_replace=True)
        )
        self.find_next_shortcut = QShortcut(QKeySequence("F3"), self)
        self.find_next_shortcut.activated.connect(self.find_next)
        self.find_previous_shortcut = QShortcut(QKeySequence("Shift+F3"), self)
        self.find_previous_shortcut.activated.connect(self.find_previous)

        self.analysis_progress = QProgressBar()
        self.analysis_progress.setRange(0, 100)
        self.analysis_progress.setValue(0)
        self.analysis_progress.setTextVisible(True)
        self.analysis_progress.setVisible(False)
        root.addWidget(self.analysis_progress)

        self.preview_card = QFrame()
        self.preview_card.setObjectName("blockCard")
        preview_layout = QVBoxLayout(self.preview_card)
        preview_layout.setContentsMargins(14, 12, 14, 14)
        preview_layout.setSpacing(9)

        preview_header = QHBoxLayout()
        self.preview_title = QLabel("실시간 분석 미리보기")
        self.preview_title.setObjectName("sectionTitle")
        self.preview_count = QLabel("0자")
        self.preview_count.setObjectName("muted")
        preview_header.addWidget(self.preview_title)
        preview_header.addStretch(1)
        preview_header.addWidget(self.preview_count)

        self.preview_editor = TimelineTextEdit()
        self.preview_editor.setReadOnly(True)
        self.preview_editor.setPlaceholderText("첫 번째 음성 인식 구간을 처리하고 있습니다…")
        self.preview_editor.setToolTip(
            "임시 타임라인의 타임스탬프도 더블클릭해 영상을 확인할 수 있습니다."
        )
        self.preview_editor.timestamp_activated.connect(self.seek_to_timestamp)
        self.preview_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.preview_editor.setMinimumHeight(170)
        self.preview_editor.setMaximumHeight(280)
        preview_layout.addLayout(preview_header)
        preview_layout.addWidget(self.preview_editor)
        self.preview_card.setVisible(False)
        root.addWidget(self.preview_card)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll_content = QWidget()
        self.blocks_layout = QVBoxLayout(self.scroll_content)
        self.blocks_layout.setContentsMargins(0, 0, 6, 8)
        self.blocks_layout.setSpacing(12)
        self.blocks_layout.addStretch(1)
        self.scroll.setWidget(self.scroll_content)

        self.review_player = SoopReviewPlayer(vod)
        self.review_player.setVisible(False)
        self.review_player.closed.connect(self._on_review_player_closed)
        self.review_player.seek_completed.connect(self._on_seek_completed)
        self.review_player.status_changed.connect(self._set_player_status)

        self.content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.addWidget(self.scroll)
        self.content_splitter.addWidget(self.review_player)
        self.content_splitter.setStretchFactor(0, 1)
        self.content_splitter.setStretchFactor(1, 0)
        root.addWidget(self.content_splitter, 1)

        self.status_label = QLabel("변경 내용은 자동으로 저장됩니다.")
        self.status_label.setObjectName("statusText")
        root.addWidget(self.status_label)

        self.set_analyzer_availability(
            analyzer_available,
            analyzer_unavailable_reason,
        )
        self.set_style_availability(style_available, style_unavailable_reason)
        self.set_text(text)

    def _build_find_replace_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("editorCard")
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(8)

        find_row = QHBoxLayout()
        find_label = QLabel("찾기")
        find_label.setObjectName("sectionTitle")
        self.find_input = QLineEdit()
        self.find_input.setPlaceholderText("찾을 단어 또는 문장")
        self.find_input.setClearButtonEnabled(True)
        self.find_input.textChanged.connect(
            lambda: self._refresh_find_matches(reset=True)
        )
        self.find_input.returnPressed.connect(self.find_next)
        self.match_label = QLabel("검색어를 입력하세요")
        self.match_label.setObjectName("muted")
        self.case_sensitive_check = QCheckBox("대소문자 구분")
        self.case_sensitive_check.toggled.connect(
            lambda: self._refresh_find_matches(reset=True)
        )
        previous_button = QPushButton("이전")
        previous_button.clicked.connect(self.find_previous)
        next_button = QPushButton("다음")
        next_button.clicked.connect(self.find_next)
        close_button = QPushButton("닫기")
        close_button.clicked.connect(self.hide_find_replace)

        find_row.addWidget(find_label)
        find_row.addWidget(self.find_input, 1)
        find_row.addWidget(self.match_label)
        find_row.addWidget(self.case_sensitive_check)
        find_row.addWidget(previous_button)
        find_row.addWidget(next_button)
        find_row.addWidget(close_button)
        layout.addLayout(find_row)

        replace_row = QHBoxLayout()
        replace_label = QLabel("바꾸기")
        replace_label.setObjectName("sectionTitle")
        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText("바꿀 내용 · 비워 두면 삭제")
        self.replace_input.returnPressed.connect(self.replace_current_match)
        replace_button = QPushButton("현재 항목 변경")
        replace_button.clicked.connect(self.replace_current_match)
        replace_all_button = QPushButton("모두 변경")
        replace_all_button.setObjectName("primaryButton")
        replace_all_button.clicked.connect(self.replace_all_matches)
        self.undo_replace_button = QPushButton("변경 되돌리기")
        self.undo_replace_button.setEnabled(False)
        self.undo_replace_button.clicked.connect(self.undo_find_replace)

        replace_row.addWidget(replace_label)
        replace_row.addWidget(self.replace_input, 1)
        replace_row.addWidget(replace_button)
        replace_row.addWidget(replace_all_button)
        replace_row.addWidget(self.undo_replace_button)
        layout.addLayout(replace_row)
        return bar

    def set_analyzer_availability(self, available: bool, reason: str = "") -> None:
        self._analyzer_available = available
        self.analyze_button.setEnabled(available)
        self.analyze_button.setVisible(not self._is_live)
        self.analyze_button.setToolTip("" if available else reason)
        if self._is_live:
            self.notice.setText(
                "라이브 연결 시 화면의 방송 경과시간을 시작 기준으로 사용합니다. "
                "약 15초 단위로 로컬 Whisper 자막이 표시되고, Gemini 임시 "
                "타임라인은 첫 1분 이후 약 3분 간격으로 갱신됩니다. "
                "SOOP 라이브에는 오디오 전용 주소가 없어 저화질 스트림을 "
                "메모리에서 수신하되 오디오만 해독하며 파일은 저장하지 않습니다."
            )
            return
        if available:
            self.notice.setText(
                "AI 분석을 누르면 공개 VOD의 오디오 전용 스트림을 고속으로 읽습니다. "
                "영상과 오디오 파일은 저장하지 않으며, 로컬 Whisper가 만든 "
                "타임스탬프 자막만 Gemini에 전송됩니다."
            )
        else:
            self.notice.setText(
                f"AI 분석을 사용하려면 설정을 완료하세요. {reason}".strip()
            )

    def set_style_availability(self, available: bool, reason: str = "") -> None:
        self._style_available = available
        self.style_button.setEnabled(available)
        self.style_button.setToolTip(
            "Whisper 재분석 없이 현재 타임라인만 건조한 제목형으로 교정합니다."
            if available
            else reason
        )

    def set_analysis_progress(self, percent: int, message: str) -> None:
        self.analysis_progress.setValue(percent)
        self.status_label.setText(message)

    def set_analysis_preview(self, stage: str, text: str) -> None:
        if stage == "timeline":
            title = "Gemini 임시 타임라인 · 최종 정리 전"
        else:
            title = "실시간 음성 인식 자막 · 최종 타임라인 아님"

        scroll_bar = self.preview_editor.verticalScrollBar()
        follow_latest = scroll_bar.value() >= scroll_bar.maximum() - 4
        self.preview_title.setText(title)
        self.preview_editor.setPlainText(text)
        self.preview_count.setText(f"{len(text):,}자")
        self.preview_card.setVisible(True)
        if follow_latest:
            cursor = self.preview_editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.preview_editor.setTextCursor(cursor)

    def set_analysis_running(self, running: bool) -> None:
        self.analyze_button.setVisible(not running)
        self.cancel_analysis_button.setVisible(running)
        self.analysis_progress.setVisible(running)
        self.copy_all_button.setEnabled(not running)
        self.ready_button.setEnabled(not running)
        self.style_button.setEnabled(not running and self._style_available)
        if running:
            self.preview_editor.clear()
            self.preview_count.setText("0자")
            self.preview_title.setText("실시간 분석 미리보기 · 첫 구간 처리 중")
            self.preview_card.setVisible(True)
        elif self.preview_card.isVisible():
            self.preview_title.setText("분석 중간 결과 · 최종본 아님")

    def set_live_running(self, running: bool) -> None:
        self.analyze_button.setVisible(False)
        self.cancel_analysis_button.setVisible(running)
        self.cancel_analysis_button.setText(
            "라이브 종료 요청됨…" if not running else "라이브 종료 및 정리"
        )
        self.cancel_analysis_button.setEnabled(running)
        self.analysis_progress.setVisible(running)
        if running:
            self.analysis_progress.setRange(0, 0)
        else:
            self.analysis_progress.setRange(0, 100)
            self.analysis_progress.setValue(100)
        self.copy_all_button.setEnabled(not running)
        self.ready_button.setEnabled(not running)
        self.style_button.setEnabled(not running and self._style_available)
        self.preview_card.setVisible(running or self.preview_card.isVisible())
        if running:
            self.preview_title.setText("실시간 음성 인식 자막 · 첫 구간 수신 중")
            self.preview_editor.clear()
            self.preview_count.setText("0자")
            self.status_label.setText(
                "라이브 오디오를 연결하고 방송 경과시간 기준점을 확인합니다…"
            )

    def request_live_stop(self) -> None:
        self.cancel_analysis_button.setEnabled(False)
        self.cancel_analysis_button.setText("라이브 종료 요청됨…")
        self.status_label.setText(
            "현재 오디오 구간을 마친 뒤 Gemini 최종 타임라인을 정리합니다…"
        )

    def set_style_running(self, running: bool) -> None:
        self.style_button.setEnabled(not running and self._style_available)
        self.style_button.setText("문체 교정 중…" if running else "Gemini 문체 교정")
        self.analyze_button.setEnabled(not running and self._analyzer_available)
        self.copy_all_button.setEnabled(not running)
        self.ready_button.setEnabled(not running)
        if running:
            self.status_label.setText(
                "Gemini가 타임스탬프를 유지하며 문체만 건조하게 교정합니다…"
            )

    def apply_analysis_result(self, text: str) -> None:
        self.set_text(text)
        self.preview_card.setVisible(False)
        self._emit_document_changed()
        self.status_label.setText("AI 타임라인 생성 완료 · 내용을 검수하세요.")

    def apply_style_result(self, text: str) -> None:
        self.set_text(text)
        self._emit_document_changed()
        self.status_label.setText("Gemini 문체 교정 완료 · 내용을 검수하세요.")

    def apply_live_update(self, stage: str, text: str) -> None:
        if stage == "live_timeline":
            self.set_text(text)
            self.status_label.setText(
                "Gemini 임시 타임라인 갱신 · 라이브 음성 인식은 계속 진행 중"
            )
            return
        self.preview_title.setText("실시간 음성 인식 자막 · 약 15초 단위 갱신")
        self.preview_editor.setPlainText(text)
        self.preview_count.setText(f"{len(text):,}자")
        self.preview_card.setVisible(True)
        cursor = self.preview_editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.preview_editor.setTextCursor(cursor)

    def apply_live_result(self, text: str) -> None:
        self.set_text(text)
        self.set_live_running(False)
        self.preview_card.setVisible(False)
        self._emit_document_changed()
        self.status_label.setText("라이브 타임라인 생성 완료 · 내용을 검수하세요.")

    def text(self) -> str:
        return "".join(block.text() for block in self._blocks)

    def blocks(self) -> list[str]:
        return [block.text() for block in self._blocks]

    def set_text(self, text: str) -> None:
        self._replace_blocks(split_timeline(text))

    def toggle_find_replace(self) -> None:
        if self.find_replace_bar.isVisible():
            self.hide_find_replace()
        else:
            self.show_find_replace()

    def show_find_replace(self, focus_replace: bool = False) -> None:
        self.find_replace_bar.setVisible(True)
        self._refresh_find_matches(reset=False)
        target = self.replace_input if focus_replace else self.find_input
        target.setFocus()
        target.selectAll()

    def hide_find_replace(self) -> None:
        self.find_replace_bar.setVisible(False)

    def find_next(self) -> None:
        self._navigate_find(1)

    def find_previous(self) -> None:
        self._navigate_find(-1)

    def _navigate_find(self, direction: int) -> None:
        self._refresh_find_matches(reset=False)
        if not self.find_input.text():
            self.status_label.setText("찾을 단어나 문장을 입력하세요.")
            return
        if not self._search_matches:
            self.status_label.setText("일치하는 내용을 찾지 못했습니다.")
            return

        if self._search_index < 0:
            anchor = self._global_cursor_position()
            if direction > 0:
                next_indexes = [
                    index
                    for index, (start, _) in enumerate(self._search_matches)
                    if start >= anchor
                ]
                self._search_index = next_indexes[0] if next_indexes else 0
            else:
                previous_indexes = [
                    index
                    for index, (_, end) in enumerate(self._search_matches)
                    if end <= anchor
                ]
                self._search_index = (
                    previous_indexes[-1]
                    if previous_indexes
                    else len(self._search_matches) - 1
                )
        else:
            self._search_index = (
                self._search_index + direction
            ) % len(self._search_matches)
        self._select_search_match(self._search_index)

    def _refresh_find_matches(self, *, reset: bool) -> None:
        previous_start: int | None = None
        if 0 <= self._search_index < len(self._search_matches):
            previous_start = self._search_matches[self._search_index][0]
        self._search_matches = find_literal_matches(
            self.text(),
            self.find_input.text(),
            case_sensitive=self.case_sensitive_check.isChecked(),
        )
        if reset:
            self._search_index = -1
        elif previous_start is not None:
            self._search_index = next(
                (
                    index
                    for index, (start, _) in enumerate(self._search_matches)
                    if start == previous_start
                ),
                -1,
            )
        elif self._search_index >= len(self._search_matches):
            self._search_index = -1
        self._update_match_label()

    def _update_match_label(self) -> None:
        if not self.find_input.text():
            self.match_label.setText("검색어를 입력하세요")
        elif not self._search_matches:
            self.match_label.setText("0개")
        elif 0 <= self._search_index < len(self._search_matches):
            self.match_label.setText(
                f"{self._search_index + 1} / {len(self._search_matches)}개"
            )
        else:
            self.match_label.setText(f"{len(self._search_matches)}개")

    def _global_cursor_position(self) -> int:
        consumed = 0
        for block in self._blocks:
            if block.editor.hasFocus():
                return consumed + block.editor.textCursor().position()
            consumed += len(block.text())
        return 0

    def _select_search_match(self, index: int) -> None:
        if not 0 <= index < len(self._search_matches):
            return
        start, end = self._search_matches[index]
        consumed = 0
        for block_index, block in enumerate(self._blocks):
            block_end = consumed + len(block.text())
            if start < block_end or block_index == len(self._blocks) - 1:
                local_start = max(0, start - consumed)
                local_end = min(len(block.text()), max(local_start, end - consumed))
                cursor = block.editor.textCursor()
                cursor.setPosition(local_start)
                cursor.setPosition(local_end, QTextCursor.MoveMode.KeepAnchor)
                block.editor.setTextCursor(cursor)
                block.editor.setFocus()
                block.editor.ensureCursorVisible()
                self.scroll.ensureWidgetVisible(block, 0, 70)
                break
            consumed = block_end
        self._update_match_label()
        self.status_label.setText(
            f"검색 결과 {index + 1}/{len(self._search_matches)}"
        )

    def replace_current_match(self) -> None:
        self._refresh_find_matches(reset=False)
        if not self._search_matches:
            self.status_label.setText("변경할 검색 결과가 없습니다.")
            return
        if not 0 <= self._search_index < len(self._search_matches):
            self.find_next()
        if not 0 <= self._search_index < len(self._search_matches):
            return

        start, end = self._search_matches[self._search_index]
        original = self.text()
        replacement = self.replace_input.text()
        updated = original[:start] + replacement + original[end:]
        self._apply_find_replace_text(original, updated)
        self._refresh_find_matches(reset=True)

        next_position = start + len(replacement)
        next_index = next(
            (
                index
                for index, (match_start, _) in enumerate(self._search_matches)
                if match_start >= next_position
            ),
            -1,
        )
        if next_index >= 0:
            self._search_index = next_index
            self._select_search_match(next_index)
        else:
            self._update_match_label()
        self.status_label.setText("현재 검색 결과를 변경했습니다.")

    def replace_all_matches(self) -> None:
        query = self.find_input.text()
        if not query:
            self.status_label.setText("찾을 단어나 문장을 입력하세요.")
            return
        original = self.text()
        updated, count = replace_literal_all(
            original,
            query,
            self.replace_input.text(),
            case_sensitive=self.case_sensitive_check.isChecked(),
        )
        if count == 0:
            self.status_label.setText("변경할 검색 결과가 없습니다.")
            return
        self._apply_find_replace_text(original, updated)
        self._refresh_find_matches(reset=True)
        self.status_label.setText(f"전체 문서에서 {count:,}곳을 변경했습니다.")

    def _apply_find_replace_text(self, original: str, updated: str) -> None:
        if original == updated:
            return
        self._find_replace_undo_text = original
        self.undo_replace_button.setEnabled(True)
        self.set_text(updated)
        self._save_timer.start()

    def undo_find_replace(self) -> None:
        if self._find_replace_undo_text is None:
            return
        previous = self._find_replace_undo_text
        self._find_replace_undo_text = None
        self.undo_replace_button.setEnabled(False)
        self.set_text(previous)
        self._save_timer.start()
        self._refresh_find_matches(reset=True)
        self.status_label.setText("마지막 찾기·바꾸기 작업을 되돌렸습니다.")

    def copy_all(self) -> None:
        QApplication.clipboard().setText(self.text())
        self.status_label.setText("전체 타임라인을 클립보드에 복사했습니다.")

    def toggle_review_player(self) -> None:
        if self._is_live:
            QDesktopServices.openUrl(QUrl(self.vod.url))
            return
        if self.review_player.isVisible():
            self.review_player.close_player()
            return
        self._show_review_player()
        self.review_player.open_player()

    def seek_to_timestamp(self, seconds: int) -> None:
        if self._is_live:
            del seconds
            self.status_label.setText(
                "라이브 세션의 타임스탬프는 방송 경과시간 기준이며 내부 검수 이동은 지원하지 않습니다."
            )
            return
        self._show_review_player()
        self.review_player.seek_to(seconds)

    def _show_review_player(self) -> None:
        was_hidden = not self.review_player.isVisible()
        self.review_player.setVisible(True)
        self.player_button.setText("검수 플레이어 닫기")
        if was_hidden:
            total_width = max(900, self.content_splitter.width())
            player_width = min(520, max(400, total_width * 2 // 5))
            self.content_splitter.setSizes(
                [max(420, total_width - player_width), player_width]
            )

    def _on_review_player_closed(self) -> None:
        self.player_button.setText("검수 플레이어 열기")
        self.status_label.setText("SOOP 검수 플레이어를 닫았습니다.")

    def _on_seek_completed(self, seconds: int) -> None:
        del seconds

    def _set_player_status(self, message: str) -> None:
        self.status_label.setText(message)

    def copy_block(self, index: int) -> None:
        if not 0 <= index < len(self._blocks):
            return
        QApplication.clipboard().setText(self._blocks[index].text())
        label = "댓글" if index == 0 else f"대댓글 {index}"
        self.status_label.setText(f"{label} 블록을 클립보드에 복사했습니다.")

    def mark_review_complete(self) -> None:
        if not self.text().strip():
            QMessageBox.information(self, "내용 없음", "검수할 타임라인 내용이 없습니다.")
            return
        self._emit_document_changed()
        self.review_completed.emit(self.vod.vod_id)
        self.status_label.setText("검수 완료로 표시했습니다. 블록별로 복사해 등록할 수 있습니다.")

    def _on_block_changed(self) -> None:
        if self._rebuilding:
            return
        self._find_replace_undo_text = None
        self.undo_replace_button.setEnabled(False)
        self._update_summary()
        if self.find_replace_bar.isVisible():
            self._refresh_find_matches(reset=False)
        self._save_timer.start()
        self._rebalance_timer.start()

    def _emit_document_changed(self) -> None:
        self.document_changed.emit(self.vod.vod_id, self.text())
        self.status_label.setText("저장됨")

    def _rebalance_blocks(self) -> None:
        if self._rebuilding or not self._blocks:
            return
        current_parts = self.blocks()
        combined = "".join(current_parts)
        next_parts = split_timeline(combined)
        if next_parts == current_parts:
            return

        focus_index = 0
        local_position = 0
        for index, block in enumerate(self._blocks):
            if block.editor.hasFocus():
                focus_index = index
                local_position = block.editor.textCursor().position()
                break
        global_position = sum(len(value) for value in current_parts[:focus_index]) + local_position

        self._replace_blocks(next_parts)

        consumed = 0
        for block in self._blocks:
            block_length = len(block.text())
            if global_position <= consumed + block_length:
                cursor = block.editor.textCursor()
                cursor.setPosition(max(0, global_position - consumed))
                block.editor.setTextCursor(cursor)
                block.editor.setFocus()
                break
            consumed += block_length

    def _replace_blocks(self, parts: list[str]) -> None:
        self._rebuilding = True
        while self.blocks_layout.count() > 1:
            item = self.blocks_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._blocks.clear()

        total = len(parts)
        for index, part in enumerate(parts):
            block = TimelineBlockWidget(index, total, part)
            block.text_changed.connect(self._on_block_changed)
            block.copy_requested.connect(self.copy_block)
            block.timestamp_activated.connect(self.seek_to_timestamp)
            self.blocks_layout.insertWidget(index, block)
            block.setVisible(True)
            self._blocks.append(block)

        self._rebuilding = False
        self._update_summary()
        if hasattr(self, "find_input"):
            self._refresh_find_matches(reset=False)
        self.blocks_layout.invalidate()
        self.blocks_layout.activate()
        self.scroll_content.updateGeometry()
        self.scroll.viewport().update()

    def _update_summary(self) -> None:
        total_chars = len(self.text())
        self.total_label.setText(
            f"전체 {total_chars:,}자  ·  댓글 블록 {len(self._blocks)}개  ·  블록당 최대 {COMMENT_LIMIT:,}자"
        )
