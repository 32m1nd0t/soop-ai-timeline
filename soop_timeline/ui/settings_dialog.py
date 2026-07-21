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
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..database import Database
from ..services.analyzer import DEFAULT_GEMINI_MODEL, DEFAULT_WHISPER_MODEL
from ..services.cache_manager import cache_size_bytes, human_size, remove_all_caches
from ..services.credentials import get_gemini_api_key, save_gemini_api_key
from ..services.gemini_timeline import DEFAULT_TOPIC_GRANULARITY
from ..services.preferences import (
    CACHE_RETENTION_SETTING,
    DISCOVERY_INTERVAL_SETTING,
    LIVE_AI_MODES,
    LIVE_AI_MODE_SETTING,
    NEW_VOD_NOTIFICATION_SETTING,
    normalized_cache_retention,
    normalized_discovery_interval,
)
from ..services.transcription import detect_whisper_runtime
from ..services.update_checker import (
    AUTO_UPDATE_CHECK_SETTING,
    UPDATE_MANIFEST_SETTING,
)
from .ai_connection_worker import AIConnectionTestWorker


class AnalysisSettingsDialog(QDialog):
    def __init__(
        self,
        database: Database,
        parent: QWidget | None = None,
        *,
        cache_actions_enabled: bool = True,
    ):
        super().__init__(parent)
        self.database = database
        self.setWindowTitle("AI 분석 설정")
        self.setMinimumWidth(580)
        self._test_thread: QThread | None = None
        self._test_worker: AIConnectionTestWorker | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(14)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        description = QLabel(
            "공개 VOD의 오디오 전용 스트림을 파일 저장 없이 고속으로 읽고, 이 PC의 "
            "faster-whisper로 음성을 인식합니다. 타임스탬프 자막과 프롬프트만 "
            "Gemini API로 전송하며, API 키는 Windows 자격 증명 관리자에 보관됩니다."
        )
        description.setWordWrap(True)
        description.setObjectName("notice")
        root.addWidget(description)

        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)

        self.api_key_input = QLineEdit(get_gemini_api_key())
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setPlaceholderText(
            "Google AI Studio에서 발급한 Gemini API 키"
        )
        form.addRow("Gemini API 키", self.api_key_input)

        self.gemini_model_input = QLineEdit(
            database.get_setting("gemini_model", DEFAULT_GEMINI_MODEL)
        )
        self.gemini_model_input.setPlaceholderText(DEFAULT_GEMINI_MODEL)
        form.addRow("Gemini 모델", self.gemini_model_input)

        test_row = QHBoxLayout()
        self.connection_test_button = QPushButton("Gemini 연결 테스트")
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

        self.live_ai_mode_combo = QComboBox()
        for mode in LIVE_AI_MODES.values():
            self.live_ai_mode_combo.addItem(
                f"{mode.label} · 시간당 약 {mode.estimated_calls_per_hour}회",
                mode.mode_id,
            )
        live_mode_index = self.live_ai_mode_combo.findData(
            database.get_setting(LIVE_AI_MODE_SETTING, "saving")
        )
        self.live_ai_mode_combo.setCurrentIndex(max(0, live_mode_index))
        form.addRow("라이브 Gemini 사용", self.live_ai_mode_combo)

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
            "처음 분석할 때 Whisper 모델 파일을 한 번 내려받습니다. 이후에는 로컬에 "
            "저장됩니다. 장시간 분석을 시작하기 전 Gemini 키·모델 권한을 짧은 "
            "요청으로 확인하며, 이 연결 확인도 소량의 Gemini API 사용량에 포함됩니다."
        )
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        root.addWidget(hint)

        discovery_title = QLabel("신규 영상 자동 확인")
        discovery_title.setObjectName("sectionTitle")
        root.addWidget(discovery_title)
        discovery_form = QFormLayout()
        self.discovery_interval_combo = QComboBox()
        for label, value in (
            ("자동 확인 끄기", 0),
            ("30분마다", 30),
            ("1시간마다", 60),
            ("3시간마다", 180),
            ("6시간마다", 360),
        ):
            self.discovery_interval_combo.addItem(label, value)
        interval_index = self.discovery_interval_combo.findData(
            normalized_discovery_interval(
                database.get_setting(DISCOVERY_INTERVAL_SETTING, "180")
            )
        )
        self.discovery_interval_combo.setCurrentIndex(max(0, interval_index))
        discovery_form.addRow("확인 주기", self.discovery_interval_combo)
        root.addLayout(discovery_form)
        self.new_vod_notification_check = QCheckBox(
            "새 영상이 발견되면 Windows 알림 표시"
        )
        self.new_vod_notification_check.setChecked(
            database.get_setting(NEW_VOD_NOTIFICATION_SETTING, "1") != "0"
        )
        root.addWidget(self.new_vod_notification_check)

        cache_title = QLabel("로컬 자막 캐시")
        cache_title.setObjectName("sectionTitle")
        root.addWidget(cache_title)
        cache_row = QHBoxLayout()
        self.cache_size_label = QLabel(f"현재 사용량: {human_size(cache_size_bytes())}")
        self.cache_size_label.setObjectName("muted")
        self.clear_cache_button = QPushButton("전체 자막 캐시 삭제")
        self.clear_cache_button.setEnabled(cache_actions_enabled)
        self.clear_cache_button.setToolTip(
            "AI 작업 중에는 삭제할 수 없습니다."
            if not cache_actions_enabled
            else "Whisper 자막과 AI 중간 체크포인트를 모두 삭제합니다."
        )
        self.clear_cache_button.clicked.connect(self._clear_cache)
        cache_row.addWidget(self.cache_size_label, 1)
        cache_row.addWidget(self.clear_cache_button)
        root.addLayout(cache_row)
        cache_form = QFormLayout()
        self.cache_retention_combo = QComboBox()
        for label, value in (
            ("자동 삭제 안 함", 0),
            ("30일 지난 캐시 삭제", 30),
            ("90일 지난 캐시 삭제", 90),
            ("180일 지난 캐시 삭제", 180),
        ):
            self.cache_retention_combo.addItem(label, value)
        retention_index = self.cache_retention_combo.findData(
            normalized_cache_retention(
                database.get_setting(CACHE_RETENTION_SETTING, "0")
            )
        )
        self.cache_retention_combo.setCurrentIndex(max(0, retention_index))
        cache_form.addRow("보관 기간", self.cache_retention_combo)
        root.addLayout(cache_form)

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
        outer.addWidget(self.buttons)

    def _start_connection_test(self) -> None:
        if self._test_thread is not None:
            return
        api_key = self.api_key_input.text().strip()
        model_name = self.gemini_model_input.text().strip()
        if not api_key:
            QMessageBox.information(self, "입력 확인", "Gemini API 키를 입력하세요.")
            return
        if not model_name:
            QMessageBox.information(self, "입력 확인", "Gemini 모델 이름을 입력하세요.")
            return

        self.connection_test_button.setEnabled(False)
        self.api_key_input.setEnabled(False)
        self.gemini_model_input.setEnabled(False)
        self.connection_status.setText("API 키와 모델 권한을 확인하는 중…")
        thread = QThread(self)
        worker = AIConnectionTestWorker(
            api_key,
            model_name,
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

    def _clear_cache(self) -> None:
        answer = QMessageBox.question(
            self,
            "전체 자막 캐시 삭제",
            "저장된 Whisper 자막과 AI 중간 결과를 모두 삭제할까요?\n"
            "기존 타임라인 문서는 유지되지만 다시 정리하려면 음성 인식부터 다시 해야 합니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = remove_all_caches()
        self.cache_size_label.setText(
            f"현재 사용량: {human_size(cache_size_bytes())}"
        )
        QMessageBox.information(
            self,
            "캐시 삭제 완료",
            f"캐시 항목 {removed:,}개를 삭제했습니다.",
        )

    def _connection_test_succeeded(self, message: str) -> None:
        self.connection_status.setText(f"✓ {message}")

    def _connection_test_failed(self, message: str) -> None:
        self.connection_status.setText(f"연결 실패 · {message}")

    def _connection_test_finished(self) -> None:
        self._test_thread = None
        self._test_worker = None
        self.connection_test_button.setEnabled(True)
        self.api_key_input.setEnabled(True)
        self.gemini_model_input.setEnabled(True)

    def _save(self) -> None:
        if self._test_thread is not None:
            QMessageBox.information(self, "연결 테스트 중", "연결 테스트가 끝난 뒤 저장하세요.")
            return
        model_name = self.gemini_model_input.text().strip()
        if not model_name:
            QMessageBox.information(self, "입력 확인", "Gemini 모델 이름을 입력하세요.")
            return
        try:
            save_gemini_api_key(self.api_key_input.text())
        except RuntimeError as error:
            QMessageBox.critical(self, "API 키 저장 실패", str(error))
            return

        self.database.set_setting("ai_provider", "gemini")
        self.database.set_setting("ai_model_gemini", model_name)
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
            LIVE_AI_MODE_SETTING,
            str(self.live_ai_mode_combo.currentData()),
        )
        self.database.set_setting(
            DISCOVERY_INTERVAL_SETTING,
            str(self.discovery_interval_combo.currentData()),
        )
        self.database.set_setting(
            NEW_VOD_NOTIFICATION_SETTING,
            "1" if self.new_vod_notification_check.isChecked() else "0",
        )
        self.database.set_setting(
            CACHE_RETENTION_SETTING,
            str(self.cache_retention_combo.currentData()),
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
