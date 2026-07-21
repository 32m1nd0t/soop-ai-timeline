from __future__ import annotations

from PySide6.QtCore import QThread
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..database import Database
from ..services.ai_provider import (
    AI_PROVIDER_SPECS,
    DEFAULT_AI_PROVIDER,
    normalize_ai_provider,
    provider_model_setting,
    provider_spec,
)
from ..services.analyzer import DEFAULT_GEMINI_MODEL, DEFAULT_WHISPER_MODEL
from ..services.credentials import get_ai_api_key, save_ai_api_key
from ..services.gemini_timeline import DEFAULT_TOPIC_GRANULARITY
from ..services.transcription import detect_whisper_runtime
from ..services.update_checker import (
    AUTO_UPDATE_CHECK_SETTING,
    UPDATE_MANIFEST_SETTING,
)
from .ai_connection_worker import AIConnectionTestWorker


class AnalysisSettingsDialog(QDialog):
    def __init__(self, database: Database, parent: QWidget | None = None):
        super().__init__(parent)
        self.database = database
        self.setWindowTitle("AI 분석 설정")
        self.setMinimumWidth(580)
        self._test_thread: QThread | None = None
        self._test_worker: AIConnectionTestWorker | None = None

        selected = normalize_ai_provider(
            database.get_setting("ai_provider", DEFAULT_AI_PROVIDER)
        )
        self._provider_values: dict[str, dict[str, str]] = {}
        for provider_id, spec in AI_PROVIDER_SPECS.items():
            legacy_default = (
                database.get_setting("gemini_model", DEFAULT_GEMINI_MODEL)
                if provider_id == "gemini"
                else spec.default_model
            )
            self._provider_values[provider_id] = {
                "api_key": get_ai_api_key(provider_id),
                "model": database.get_setting(
                    provider_model_setting(provider_id),
                    legacy_default,
                ),
            }
        self._current_provider = selected

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)

        description = QLabel(
            "공개 VOD의 오디오 전용 스트림을 파일 저장 없이 고속으로 읽고, 이 PC의 "
            "faster-whisper로 음성을 인식합니다. 타임스탬프 자막과 프롬프트만 선택한 "
            "AI API로 전송하며, 공급자별 API 키는 Windows 자격 증명 관리자에 따로 보관됩니다."
        )
        description.setWordWrap(True)
        description.setObjectName("notice")
        root.addWidget(description)

        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)

        self.provider_combo = QComboBox()
        for provider_id, spec in AI_PROVIDER_SPECS.items():
            self.provider_combo.addItem(spec.display_name, provider_id)
        provider_index = self.provider_combo.findData(selected)
        self.provider_combo.setCurrentIndex(max(0, provider_index))
        form.addRow("타임라인 AI", self.provider_combo)

        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("API 키", self.api_key_input)

        self.model_input = QLineEdit()
        form.addRow("모델", self.model_input)

        test_row = QHBoxLayout()
        self.connection_test_button = QPushButton("AI 연결 테스트")
        self.connection_test_button.clicked.connect(self._start_connection_test)
        self.connection_status = QLabel("분석 전 연결 테스트를 권장합니다.")
        self.connection_status.setObjectName("muted")
        self.connection_status.setWordWrap(True)
        test_row.addWidget(self.connection_test_button)
        test_row.addWidget(self.connection_status, 1)
        form.addRow("연결 확인", test_row)

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
        form.addRow("기본 타임라인 밀도", self.topic_granularity_combo)

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

        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        self._load_provider_values(selected)

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
            "처음 분석할 때 Whisper 모델 파일을 한 번 내려받습니다. 이후에는 로컬에 "
            "저장됩니다. 장시간 분석을 시작하기 전 선택한 AI의 키·모델 권한을 짧은 "
            "요청으로 확인하며, 이 연결 확인도 해당 공급자의 소량 API 사용량에 포함됩니다."
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
            "GitHub Release를 자동 확인합니다. 파일을 자동 설치하지 않고 새 버전과 "
            "다운로드 페이지만 알려줍니다."
        )
        update_hint.setWordWrap(True)
        update_hint.setObjectName("muted")
        root.addWidget(update_hint)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

    def _capture_current_provider(self) -> None:
        self._provider_values[self._current_provider] = {
            "api_key": self.api_key_input.text(),
            "model": self.model_input.text().strip(),
        }

    def _load_provider_values(self, provider: str) -> None:
        spec = provider_spec(provider)
        values = self._provider_values[provider]
        self.api_key_input.setText(values["api_key"])
        self.api_key_input.setPlaceholderText(spec.key_placeholder)
        self.model_input.setText(values["model"] or spec.default_model)
        self.model_input.setPlaceholderText(spec.default_model)
        self.connection_status.setText(
            f"{spec.display_name} 키와 모델을 확인하려면 연결 테스트를 누르세요."
        )

    def _provider_changed(self, *_: object) -> None:
        if self._test_thread is not None:
            return
        self._capture_current_provider()
        self._current_provider = normalize_ai_provider(
            str(self.provider_combo.currentData())
        )
        self._load_provider_values(self._current_provider)

    def _start_connection_test(self) -> None:
        if self._test_thread is not None:
            return
        self._capture_current_provider()
        values = self._provider_values[self._current_provider]
        if not values["api_key"].strip():
            QMessageBox.information(self, "입력 확인", "API 키를 입력하세요.")
            return
        if not values["model"].strip():
            QMessageBox.information(self, "입력 확인", "모델 이름을 입력하세요.")
            return

        self.connection_test_button.setEnabled(False)
        self.provider_combo.setEnabled(False)
        self.connection_status.setText("API 키와 모델 권한을 확인하는 중…")
        thread = QThread(self)
        worker = AIConnectionTestWorker(
            self._current_provider,
            values["api_key"],
            values["model"],
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._connection_test_succeeded)
        worker.failed.connect(self._connection_test_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._connection_test_finished)
        thread.finished.connect(thread.deleteLater)
        self._test_thread = thread
        self._test_worker = worker
        thread.start()

    def _connection_test_succeeded(self, message: str) -> None:
        self.connection_status.setText(f"✓ {message}")

    def _connection_test_failed(self, message: str) -> None:
        self.connection_status.setText(f"연결 실패 · {message}")

    def _connection_test_finished(self) -> None:
        self._test_thread = None
        self._test_worker = None
        self.connection_test_button.setEnabled(True)
        self.provider_combo.setEnabled(True)

    def _save(self) -> None:
        if self._test_thread is not None:
            QMessageBox.information(self, "연결 테스트 중", "연결 테스트가 끝난 뒤 저장하세요.")
            return
        self._capture_current_provider()
        selected_values = self._provider_values[self._current_provider]
        if not selected_values["model"].strip():
            QMessageBox.information(self, "입력 확인", "AI 모델 이름을 입력하세요.")
            return
        try:
            for provider, values in self._provider_values.items():
                save_ai_api_key(provider, values["api_key"])
        except RuntimeError as error:
            QMessageBox.critical(self, "API 키 저장 실패", str(error))
            return

        self.database.set_setting("ai_provider", self._current_provider)
        for provider, values in self._provider_values.items():
            model = values["model"].strip() or provider_spec(provider).default_model
            self.database.set_setting(provider_model_setting(provider), model)
            if provider == "gemini":
                self.database.set_setting("gemini_model", model)
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

    def reject(self) -> None:
        if self._test_thread is not None:
            QMessageBox.information(
                self,
                "연결 테스트 중",
                "연결 테스트가 끝난 뒤 창을 닫아 주세요.",
            )
            return
        super().reject()
