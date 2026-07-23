from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..models import Vod
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

        self.editor = QPlainTextEdit(self._text)
        self.editor.setReadOnly(True)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        root.addWidget(self.editor, 1)

        buttons = QHBoxLayout()
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
        buttons.addWidget(copy_button)
        buttons.addWidget(txt_button)
        buttons.addWidget(srt_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        root.addLayout(buttons)

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
