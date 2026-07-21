from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ..database import Database
from .. import __version__
from ..services.analyzer import DEFAULT_GEMINI_MODEL, DEFAULT_WHISPER_MODEL
from ..services.credentials import get_gemini_api_key, save_gemini_api_key
from ..services.gemini_timeline import DEFAULT_TOPIC_GRANULARITY
from ..services.transcription import detect_whisper_runtime
from ..services.update_checker import (
    AUTO_UPDATE_CHECK_SETTING,
    UPDATE_MANIFEST_SETTING,
)


class AnalysisSettingsDialog(QDialog):
    def __init__(self, database: Database, parent: QWidget | None = None):
        super().__init__(parent)
        self.database = database
        self.setWindowTitle("AI 분석 설정")
        self.setMinimumWidth(520)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)

        description = QLabel(
            "공개 VOD의 오디오 전용 스트림을 파일 저장 없이 고속으로 읽고, 이 PC의 "
            "faster-whisper로 배치 인식합니다. 타임스탬프 자막만 Gemini API로 "
            "전송하며 API 키는 Windows 자격 증명 관리자에 보관됩니다."
        )
        description.setWordWrap(True)
        description.setObjectName("notice")
        root.addWidget(description)

        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)

        self.api_key_input = QLineEdit(get_gemini_api_key())
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setPlaceholderText("Google AI Studio에서 발급한 Gemini API 키")
        form.addRow("Gemini API 키", self.api_key_input)

        self.gemini_model_input = QLineEdit(
            database.get_setting("gemini_model", DEFAULT_GEMINI_MODEL)
        )
        self.gemini_model_input.setPlaceholderText(DEFAULT_GEMINI_MODEL)
        form.addRow("Gemini 모델", self.gemini_model_input)

        self.topic_granularity_combo = QComboBox()
        self.topic_granularity_combo.addItem(
            "큰 주제 위주 · 같은 토크를 한 줄로 묶기",
            "broad",
        )
        self.topic_granularity_combo.addItem(
            "기본 · 명확한 소주제 전환만 분리",
            "balanced",
        )
        self.topic_granularity_combo.addItem(
            "촘촘하게 · 세부 화제도 분리",
            "detailed",
        )
        current_granularity = database.get_setting(
            "topic_granularity",
            DEFAULT_TOPIC_GRANULARITY,
        )
        granularity_index = self.topic_granularity_combo.findData(
            current_granularity
        )
        self.topic_granularity_combo.setCurrentIndex(max(0, granularity_index))
        form.addRow("타임라인 밀도", self.topic_granularity_combo)

        self.whisper_model_combo = QComboBox()
        self.whisper_model_combo.addItem("large-v3-turbo · 속도 우선", "large-v3-turbo")
        self.whisper_model_combo.addItem("large-v3 · 정확도 우선", "large-v3")
        current_whisper = database.get_setting("whisper_model", DEFAULT_WHISPER_MODEL)
        index = self.whisper_model_combo.findData(current_whisper)
        self.whisper_model_combo.setCurrentIndex(max(0, index))
        form.addRow("Whisper 모델", self.whisper_model_combo)

        self.whisper_device_combo = QComboBox()
        self.whisper_device_combo.addItem("자동 · GPU 우선, 없으면 CPU", "auto")
        self.whisper_device_combo.addItem("NVIDIA GPU · CUDA float16", "cuda")
        self.whisper_device_combo.addItem("CPU · int8", "cpu")
        current_device = database.get_setting("whisper_device", "auto")
        device_index = self.whisper_device_combo.findData(current_device)
        self.whisper_device_combo.setCurrentIndex(max(0, device_index))
        form.addRow("연산 장치", self.whisper_device_combo)
        root.addLayout(form)

        try:
            runtime = detect_whisper_runtime("auto")
            runtime_text = f"현재 감지: {runtime.description}"
            if runtime.warning:
                runtime_text += f"\n{runtime.warning}"
        except RuntimeError as error:
            runtime_text = str(error)
        runtime_label = QLabel(runtime_text)
        runtime_label.setWordWrap(True)
        runtime_label.setObjectName("muted")
        root.addWidget(runtime_label)

        hint = QLabel(
            "처음 분석할 때 Whisper 모델 파일을 한 번 내려받습니다. "
            "이후에는 로컬에 저장되어 추가 API 사용량이 발생하지 않습니다. "
            "고속 스트림을 얻지 못해도 1배속 캡처로 전환하지 않습니다."
        )
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        root.addWidget(hint)

        update_title = QLabel(f"앱 업데이트 · 현재 버전 {__version__}")
        update_title.setObjectName("sectionTitle")
        root.addWidget(update_title)

        self.auto_update_check = QCheckBox("앱 실행 시 새 버전 자동 확인")
        self.auto_update_check.setChecked(
            database.get_setting(AUTO_UPDATE_CHECK_SETTING, "1") != "0"
        )
        root.addWidget(self.auto_update_check)

        update_form = QFormLayout()
        self.update_manifest_input = QLineEdit(
            database.get_setting(UPDATE_MANIFEST_SETTING, "")
        )
        self.update_manifest_input.setPlaceholderText(
            "비워 두면 공식 GitHub Releases 주소 사용"
        )
        update_form.addRow("업데이트 주소 재정의", self.update_manifest_input)
        root.addLayout(update_form)

        update_hint = QLabel(
            "일반 사용자는 비워 두면 됩니다. 32m1nd0t/soop-ai-timeline의 최신 "
            "GitHub Release를 자동 확인합니다. 다른 배포 주소를 사용할 때만 이 값을 "
            "입력하세요. 파일을 자동 설치하지 않고 새 버전과 다운로드 페이지만 알려줍니다."
        )
        update_hint.setWordWrap(True)
        update_hint.setObjectName("muted")
        root.addWidget(update_hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _save(self) -> None:
        model_name = self.gemini_model_input.text().strip()
        if not model_name:
            QMessageBox.information(self, "입력 확인", "Gemini 모델 이름을 입력하세요.")
            return
        try:
            save_gemini_api_key(self.api_key_input.text())
        except RuntimeError as error:
            QMessageBox.critical(self, "API 키 저장 실패", str(error))
            return
        self.database.set_setting("gemini_model", model_name)
        self.database.set_setting(
            "whisper_model", str(self.whisper_model_combo.currentData())
        )
        self.database.set_setting(
            "whisper_device", str(self.whisper_device_combo.currentData())
        )
        self.database.set_setting(
            "topic_granularity",
            str(self.topic_granularity_combo.currentData()),
        )
        self.database.set_setting(
            AUTO_UPDATE_CHECK_SETTING,
            "1" if self.auto_update_check.isChecked() else "0",
        )
        self.database.set_setting(
            UPDATE_MANIFEST_SETTING,
            self.update_manifest_input.text().strip(),
        )
        self.accept()
