from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..models import TimelineRevision


class TimelineVersionHistoryDialog(QDialog):
    def __init__(
        self,
        revisions: list[TimelineRevision],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.revisions = revisions
        self.restored_text: str | None = None
        self.setWindowTitle("타임라인 버전 기록")
        self.resize(820, 560)

        root = QVBoxLayout(self)
        root.addWidget(
            QLabel("AI 재분석·주제 재묶기·일괄 변경 전에 저장된 버전입니다.")
        )

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.revision_list = QListWidget()
        for revision in revisions:
            item = QListWidgetItem(
                f"{_display_datetime(revision.created_at)}\n{revision.reason}"
            )
            item.setData(Qt.ItemDataRole.UserRole, revision.id)
            self.revision_list.addItem(item)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        splitter.addWidget(self.revision_list)
        splitter.addWidget(self.preview)
        splitter.setSizes([260, 560])
        root.addWidget(splitter, 1)

        buttons = QHBoxLayout()
        export_button = QPushButton("선택 버전 TXT 저장")
        export_button.clicked.connect(self._export_selected)
        restore_button = QPushButton("이 버전으로 복원")
        restore_button.setObjectName("primaryButton")
        restore_button.clicked.connect(self._restore_selected)
        close_button = QPushButton("닫기")
        close_button.clicked.connect(self.reject)
        buttons.addWidget(export_button)
        buttons.addStretch(1)
        buttons.addWidget(restore_button)
        buttons.addWidget(close_button)
        root.addLayout(buttons)

        self.revision_list.currentItemChanged.connect(self._selection_changed)
        if revisions:
            self.revision_list.setCurrentRow(0)

    def _selected_revision(self) -> TimelineRevision | None:
        item = self.revision_list.currentItem()
        if item is None:
            return None
        revision_id = int(item.data(Qt.ItemDataRole.UserRole))
        return next(
            (revision for revision in self.revisions if revision.id == revision_id),
            None,
        )

    def _selection_changed(self, *_: object) -> None:
        revision = self._selected_revision()
        self.preview.setPlainText(revision.text if revision is not None else "")

    def _restore_selected(self) -> None:
        revision = self._selected_revision()
        if revision is None:
            QMessageBox.information(self, "선택 필요", "복원할 버전을 선택하세요.")
            return
        self.restored_text = revision.text
        self.accept()

    def _export_selected(self) -> None:
        revision = self._selected_revision()
        if revision is None:
            QMessageBox.information(self, "선택 필요", "저장할 버전을 선택하세요.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "타임라인 버전 저장",
            f"timeline-revision-{revision.id}.txt",
            "텍스트 파일 (*.txt);;모든 파일 (*.*)",
        )
        if not path:
            return
        try:
            Path(path).write_text(revision.text, encoding="utf-8-sig")
        except OSError as error:
            QMessageBox.critical(self, "저장 실패", str(error))


def _display_datetime(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return value
