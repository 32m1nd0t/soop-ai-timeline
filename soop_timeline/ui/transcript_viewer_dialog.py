from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..models import Vod
from ..services.text_editing import find_literal_matches
from ..services.transcript_export import (
    transcript_summary,
    transcript_to_srt,
    transcript_to_text,
)
from ..services.transcription import Transcript


class TranscriptViewerDialog(QDialog):
    def __init__(
        self,
        vod: Vod,
        transcript: Transcript,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.vod = vod
        self.transcript = transcript
        self._text = transcript_to_text(transcript)
        self._search_matches: list[tuple[int, int]] = []
        self._search_index = -1
        self.setWindowTitle("저장된 Whisper 자막")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setModal(False)
        self.resize(900, 680)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        title = QLabel(vod.title)
        title.setObjectName("sectionTitle")
        title.setWordWrap(True)
        root.addWidget(title)
        info = QLabel(transcript_summary(transcript))
        info.setObjectName("muted")
        root.addWidget(info)

        self.find_bar = self._build_find_bar()
        self.find_bar.setVisible(False)
        root.addWidget(self.find_bar)

        self.editor = QPlainTextEdit(self._text)
        self.editor.setReadOnly(True)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        root.addWidget(self.editor, 1)

        buttons = QHBoxLayout()
        find_button = QPushButton("찾기")
        find_button.setToolTip("자막에서 대사를 찾습니다. (Ctrl+F)")
        find_button.clicked.connect(self.toggle_find)
        copy_button = QPushButton("전체 자막 복사")
        copy_button.clicked.connect(
            lambda: QApplication.clipboard().setText(self._text)
        )
        txt_button = QPushButton("TXT 저장")
        txt_button.clicked.connect(self._save_txt)
        srt_button = QPushButton("SRT 저장")
        srt_button.clicked.connect(self._save_srt)
        close_button = QPushButton("닫기")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(find_button)
        buttons.addWidget(copy_button)
        buttons.addWidget(txt_button)
        buttons.addWidget(srt_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        root.addLayout(buttons)

        self.find_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        self.find_shortcut.activated.connect(self.show_find)
        self.find_next_shortcut = QShortcut(QKeySequence("F3"), self)
        self.find_next_shortcut.activated.connect(self.find_next)
        self.find_previous_shortcut = QShortcut(QKeySequence("Shift+F3"), self)
        self.find_previous_shortcut.activated.connect(self.find_previous)

    def _build_find_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("editorCard")
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 11, 14, 11)
        row.setSpacing(8)

        find_label = QLabel("찾기")
        find_label.setObjectName("sectionTitle")
        self.find_input = QLineEdit()
        self.find_input.setPlaceholderText("찾을 대사 또는 단어")
        self.find_input.setClearButtonEnabled(True)
        self.find_input.textChanged.connect(self._refresh_find_matches)
        # Enter는 QDialog까지 전달되면 기본 버튼(전체 자막 복사 등)도 눌리므로
        # 입력칸에서 직접 처리해 삼킨다.
        self.find_input.installEventFilter(self)
        self.match_label = QLabel("검색어를 입력하세요")
        self.match_label.setObjectName("muted")
        self.case_sensitive_check = QCheckBox("대소문자 구분")
        self.case_sensitive_check.toggled.connect(self._refresh_find_matches)
        previous_button = QPushButton("이전")
        previous_button.clicked.connect(self.find_previous)
        next_button = QPushButton("다음")
        next_button.clicked.connect(self.find_next)
        close_button = QPushButton("닫기")
        close_button.clicked.connect(self.hide_find)

        row.addWidget(find_label)
        row.addWidget(self.find_input, 1)
        row.addWidget(self.match_label)
        row.addWidget(self.case_sensitive_check)
        row.addWidget(previous_button)
        row.addWidget(next_button)
        row.addWidget(close_button)
        return bar

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.find_input and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self.find_previous()
                else:
                    self.find_next()
                return True
            if event.key() == Qt.Key.Key_Escape:
                self.hide_find()
                return True
        return super().eventFilter(watched, event)

    def toggle_find(self) -> None:
        if self.find_bar.isVisible():
            self.hide_find()
        else:
            self.show_find()

    def show_find(self) -> None:
        self.find_bar.setVisible(True)
        self.find_input.setFocus()
        self.find_input.selectAll()

    def hide_find(self) -> None:
        self.find_bar.setVisible(False)
        self.editor.setFocus()

    def find_next(self) -> None:
        self._navigate_find(1)

    def find_previous(self) -> None:
        self._navigate_find(-1)

    def _navigate_find(self, direction: int) -> None:
        if not self.find_bar.isVisible():
            self.show_find()
        matches = self._search_matches
        if not matches:
            return

        if self._search_index < 0:
            # 검색어를 바꾼 뒤 첫 이동은 현재 커서 위치에서 가장 가까운 결과로 간다.
            cursor = self.editor.textCursor()
            if direction > 0:
                anchor = cursor.selectionStart()
                self._search_index = next(
                    (index for index, (start, _) in enumerate(matches) if start >= anchor),
                    0,
                )
            else:
                anchor = cursor.selectionEnd()
                self._search_index = next(
                    (
                        index
                        for index in range(len(matches) - 1, -1, -1)
                        if matches[index][1] <= anchor
                    ),
                    len(matches) - 1,
                )
        else:
            self._search_index = (self._search_index + direction) % len(matches)

        start, end = matches[self._search_index]
        cursor = self.editor.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        # 화면 밖 결과는 맨 아래 줄에 걸치지 않도록 가운데로 올려 앞뒤 대사도 보이게 한다.
        visible = self.editor.viewport().rect().contains(self.editor.cursorRect(cursor))
        self.editor.setTextCursor(cursor)
        if not visible:
            self.editor.centerCursor()
        self._update_match_label()

    def _refresh_find_matches(self) -> None:
        self._search_matches = find_literal_matches(
            self._text,
            self.find_input.text(),
            case_sensitive=self.case_sensitive_check.isChecked(),
        )
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

    def _save_txt(self) -> None:
        self._save(
            self._text,
            f"{self.vod.streamer_name}-{self.vod.vod_id}-transcript.txt",
            "텍스트 파일 (*.txt)",
        )

    def _save_srt(self) -> None:
        self._save(
            transcript_to_srt(self.transcript),
            f"{self.vod.streamer_name}-{self.vod.vod_id}-transcript.srt",
            "SRT 자막 (*.srt)",
        )

    def _save(self, text: str, default_name: str, file_filter: str) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "자막 저장",
            default_name,
            f"{file_filter};;모든 파일 (*.*)",
        )
        if not path:
            return
        try:
            Path(path).write_text(text, encoding="utf-8-sig")
        except OSError as error:
            QMessageBox.critical(self, "자막 저장 실패", str(error))
