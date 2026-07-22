from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QThread, QTimer, Qt, QUrl, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStyle,
    QSystemTrayIcon,
    QTabBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..database import Database
from ..models import STATE_LABELS, Vod, VodState
from ..services.analyzer import (
    LocalWhisperGeminiAnalyzer,
    TimelineAnalyzer,
    has_pending_timeline_finalization,
    load_cached_transcript,
)
from ..services.ai_provider import estimate_timeline_calls
from ..services.cache_manager import (
    cleanup_expired_caches,
    has_vod_cache,
    remove_vod_cache,
)
from ..services.channel_id import normalize_channel_id
from ..services.discovery import SoopVodDiscovery
from ..services.diagnostics import build_diagnostic_report, create_diagnostic_bundle
from ..services.gemini_line_rewrite import AITimelineLineRewriter
from ..services.gemini_style import AITimelineStyler
from ..services.live_stream import LiveAudioSource
from ..services.manual_link import (
    ResolvedVodLink,
    parse_soop_link,
)
from ..services.preferences import (
    CACHE_RETENTION_SETTING,
    DISCOVERY_INTERVAL_SETTING,
    NEW_VOD_NOTIFICATION_SETTING,
    PRIVACY_NOTICE_SETTING,
    PRIVACY_NOTICE_VERSION,
    normalized_cache_retention,
    normalized_discovery_interval,
    setting_enabled,
)
from ..services.transcription import format_timestamp
from ..services.timeline_validation import parse_duration_text
from ..services.update_checker import (
    automatic_update_check_enabled,
    configured_manifest_url,
    parse_update_manifest,
)
from .analysis_worker import AnalysisWorker
from .line_rewrite_worker import TimelineLineRewriteWorker
from .live_worker import LiveAnalysisWorker
from .manual_link_worker import ManualLinkWorker
from .regroup_worker import TimelineRegroupWorker
from .settings_dialog import AnalysisSettingsDialog
from .style_worker import TimelineStyleWorker
from .timeline_editor import TimelineDocumentEditor
from .transcript_viewer_dialog import TranscriptViewerDialog
from .version_history_dialog import TimelineVersionHistoryDialog


class MainWindow(QMainWindow):
    def __init__(self, database: Database, parent: QWidget | None = None):
        super().__init__(parent)
        self.database = database
        self.analyzer: TimelineAnalyzer = LocalWhisperGeminiAnalyzer.from_database(database)
        self.styler = AITimelineStyler.from_database(database)
        self.discovery = SoopVodDiscovery(self)
        self._actual_new_count = 0
        self._editor_tabs: dict[str, TimelineDocumentEditor] = {}
        self._analysis_jobs: dict[str, tuple[QThread, AnalysisWorker]] = {}
        self._live_jobs: dict[str, tuple[QThread, LiveAnalysisWorker]] = {}
        self._style_jobs: dict[str, tuple[QThread, TimelineStyleWorker]] = {}
        self._line_rewrite_jobs: dict[str, tuple[QThread, TimelineLineRewriteWorker]] = {}
        self._regroup_jobs: dict[str, tuple[QThread, TimelineRegroupWorker]] = {}
        self._manual_link_job: tuple[QThread, ManualLinkWorker] | None = None
        self._analysis_queue: list[str] = self.database.recover_analysis_queue()
        self._close_after_analysis = False
        self._update_reply: QNetworkReply | None = None
        self._update_check_silent = True
        self._stale_live_sessions = self.database.recover_stale_live_sessions()
        cleanup_expired_caches(
            normalized_cache_retention(
                self.database.get_setting(CACHE_RETENTION_SETTING, "0")
            )
        )

        self.update_network = QNetworkAccessManager(self)
        self.update_timeout = QTimer(self)
        self.update_timeout.setSingleShot(True)
        self.update_timeout.setInterval(8_000)
        self.update_timeout.timeout.connect(self._abort_update_check)

        self.setWindowTitle("SOOP AI 타임라인")
        self.resize(1280, 820)
        self.setMinimumSize(980, 650)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.setCentralWidget(self.tabs)

        self.dashboard = self._build_dashboard()
        self.tabs.addTab(self.dashboard, "신규 영상")
        self.tabs.tabBar().setTabButton(0, QTabBar.ButtonPosition.RightSide, None)

        self.discovery.started.connect(self._on_discovery_started)
        self.discovery.progress.connect(self.status_label.setText)
        self.discovery.result_ready.connect(self._on_discovery_result)
        self.discovery.streamer_error.connect(self._on_discovery_error)
        self.discovery.finished.connect(self._on_discovery_finished)

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_discovery)
        self._configure_refresh_timer()

        self.tray_icon: QSystemTrayIcon | None = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            icon = self.windowIcon()
            if icon.isNull():
                icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
            self.tray_icon = QSystemTrayIcon(icon, self)
            self.tray_icon.setToolTip("SOOP AI 타임라인")
            self.tray_icon.show()

        self.load_streamers()
        self.load_vods()
        if self.database.get_setting(PRIVACY_NOTICE_SETTING, "") == PRIVACY_NOTICE_VERSION:
            self._schedule_startup_tasks()
        else:
            QTimer.singleShot(100, self._show_first_run_privacy_notice)

    def _schedule_startup_tasks(self) -> None:
        QTimer.singleShot(1_200, self._initial_refresh)
        if self._analysis_queue:
            QTimer.singleShot(1_800, self._resume_persisted_analysis)
        if automatic_update_check_enabled(self.database):
            QTimer.singleShot(2_500, lambda: self.check_for_updates(silent=True))
        if self._stale_live_sessions:
            QTimer.singleShot(
                500,
                lambda: self.status_label.setText(
                    f"비정상 종료된 라이브 세션 {len(self._stale_live_sessions):,}개를 "
                    "복구 대상으로 표시했습니다. 저장 자막 다시 정리를 사용할 수 있습니다."
                ),
            )

    def _show_first_run_privacy_notice(self) -> None:
        message = QMessageBox(self)
        message.setWindowTitle("처음 사용 전 데이터 처리 안내")
        message.setIcon(QMessageBox.Icon.Information)
        message.setText(
            "영상·오디오 파일은 저장하지 않으며 Whisper 음성 인식은 이 PC에서 처리합니다."
        )
        message.setInformativeText(
            "타임스탬프가 포함된 자막, 영상 제목, 스트리머 이름과 단어 사전은 "
            "타임라인 생성을 위해 Google Gemini API로 전송됩니다. 로컬에는 자막·AI "
            "중간 결과·타임라인 문서가 저장됩니다. 저작권자 또는 스트리머의 허용 범위와 "
            "SOOP 약관을 확인한 영상에만 사용하세요. 설정에서 캐시를 삭제할 수 있습니다."
        )
        accept_button = message.addButton("확인하고 시작", QMessageBox.ButtonRole.AcceptRole)
        message.addButton("종료", QMessageBox.ButtonRole.RejectRole)
        message.exec()
        if message.clickedButton() is not accept_button:
            self.close()
            return
        self.database.set_setting(PRIVACY_NOTICE_SETTING, PRIVACY_NOTICE_VERSION)
        self._schedule_startup_tasks()

    def _configure_refresh_timer(self) -> None:
        minutes = normalized_discovery_interval(
            self.database.get_setting(DISCOVERY_INTERVAL_SETTING, "180")
        )
        self.refresh_timer.stop()
        if minutes > 0:
            self.refresh_timer.setInterval(minutes * 60 * 1_000)
            self.refresh_timer.start()

    def _build_dashboard(self) -> QWidget:
        root_widget = QWidget()
        root_widget.setObjectName("appRoot")
        root = QVBoxLayout(root_widget)
        root.setContentsMargins(20, 18, 20, 20)
        root.setSpacing(14)

        header = QHBoxLayout()
        title_column = QVBoxLayout()
        title = QLabel("SOOP AI 타임라인")
        title.setObjectName("appTitle")
        subtitle = QLabel("신규 다시보기를 모아 보고, 선택한 영상의 타임라인을 검수합니다.")
        subtitle.setObjectName("muted")
        title_column.addWidget(title)
        title_column.addWidget(subtitle)
        header.addLayout(title_column)
        header.addStretch(1)

        self.refresh_button = QPushButton("새 영상 확인")
        self.refresh_button.clicked.connect(self.refresh_discovery)
        self.settings_button = QPushButton("AI 설정")
        self.settings_button.clicked.connect(self.open_analysis_settings)
        self.update_button = QPushButton("업데이트 확인")
        self.update_button.clicked.connect(
            lambda: self.check_for_updates(silent=False)
        )
        self.create_button = QPushButton("선택 영상 타임라인 작성")
        self.create_button.setObjectName("primaryButton")
        self.create_button.clicked.connect(self.open_selected_timelines)
        self.diagnostics_button = QPushButton("진단 정보")
        self.diagnostics_button.clicked.connect(self.show_diagnostics_options)
        header.addWidget(self.refresh_button)
        header.addWidget(self.settings_button)
        header.addWidget(self.update_button)
        header.addWidget(self.diagnostics_button)
        header.addWidget(self.create_button)
        root.addLayout(header)

        notice = QLabel(
            "현재는 검수 후 블록별 복사 방식입니다. 공식 API 권한을 받으면 로그인과 댓글·대댓글 등록 버튼을 연결합니다."
        )
        notice.setObjectName("notice")
        notice.setWordWrap(True)
        root.addWidget(notice)

        root.addWidget(self._build_manual_link_panel())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_streamer_panel())
        splitter.addWidget(self._build_vod_panel())
        splitter.setSizes([285, 950])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        status_row = QHBoxLayout()
        self.status_label = QLabel("준비됨")
        self.status_label.setObjectName("statusText")
        self.last_checked_label = QLabel("마지막 확인: 아직 없음")
        self.last_checked_label.setObjectName("muted")
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)
        status_row.addWidget(self.last_checked_label)
        root.addLayout(status_row)

        return root_widget

    def show_diagnostics_options(self) -> None:
        message = QMessageBox(self)
        message.setWindowTitle("진단 정보")
        message.setText("API 키와 자막 원문을 제외한 환경 정보와 최근 오류 로그를 만듭니다.")
        copy_button = message.addButton("클립보드 복사", QMessageBox.ButtonRole.AcceptRole)
        save_button = message.addButton("ZIP 저장", QMessageBox.ButtonRole.ActionRole)
        message.addButton("취소", QMessageBox.ButtonRole.RejectRole)
        message.exec()
        if message.clickedButton() is copy_button:
            QApplication.clipboard().setText(build_diagnostic_report(self.database))
            self.status_label.setText("진단 정보를 클립보드에 복사했습니다.")
            return
        if message.clickedButton() is not save_button:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "진단 묶음 저장",
            "soop-timeline-diagnostics.zip",
            "ZIP 파일 (*.zip)",
        )
        if not path:
            return
        try:
            create_diagnostic_bundle(path, self.database)
        except OSError as error:
            QMessageBox.critical(self, "진단 묶음 저장 실패", str(error))
            return
        self.status_label.setText("진단 묶음을 저장했습니다.")

    def _build_manual_link_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        title = QLabel("수동 링크 분석")
        title.setObjectName("sectionTitle")
        description = QLabel(
            "다시보기 링크는 해당 영상만 고속 분석하고 자동 확인 목록에는 추가하지 않습니다. "
            "라이브 링크는 입력 시점의 방송 경과시간부터 실시간 자막과 타임라인을 작성합니다."
        )
        description.setObjectName("muted")
        description.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(description)

        row = QHBoxLayout()
        self.manual_link_input = QLineEdit()
        self.manual_link_input.setPlaceholderText(
            "https://vod.sooplive.com/player/... 또는 https://play.sooplive.com/..."
        )
        self.manual_link_input.returnPressed.connect(self.resolve_manual_link)
        self.manual_link_button = QPushButton("링크 분석 시작")
        self.manual_link_button.setObjectName("primaryButton")
        self.manual_link_button.clicked.connect(self.resolve_manual_link)
        row.addWidget(self.manual_link_input, 1)
        row.addWidget(self.manual_link_button)
        layout.addLayout(row)
        return panel

    def _build_streamer_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(9)

        title = QLabel("자동 확인 스트리머")
        title.setObjectName("sectionTitle")
        description = QLabel("스트리머 아이디 또는 방송국 주소를 등록하세요.")
        description.setObjectName("muted")
        description.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(description)

        self.streamer_list = QListWidget()
        self.streamer_list.setAlternatingRowColors(True)
        layout.addWidget(self.streamer_list, 1)

        self.channel_input = QLineEdit()
        self.channel_input.setPlaceholderText("예: streamer_id 또는 방송국 URL")
        self.channel_input.returnPressed.connect(self.add_streamer)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("표시 이름 (선택)")
        self.name_input.returnPressed.connect(self.add_streamer)
        layout.addWidget(self.channel_input)
        layout.addWidget(self.name_input)

        button_row = QHBoxLayout()
        add_button = QPushButton("추가")
        add_button.setObjectName("primaryButton")
        add_button.clicked.connect(self.add_streamer)
        remove_button = QPushButton("삭제")
        remove_button.setObjectName("dangerButton")
        remove_button.clicked.connect(self.remove_streamer)
        glossary_button = QPushButton("단어 사전")
        glossary_button.setToolTip("스트리머별 인명·게임명·고유명사 표기를 등록합니다.")
        glossary_button.clicked.connect(self.edit_streamer_glossary)
        button_row.addWidget(add_button, 1)
        button_row.addWidget(glossary_button)
        button_row.addWidget(remove_button)
        layout.addLayout(button_row)

        return panel

    def _build_vod_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        controls = QHBoxLayout()
        title = QLabel("다시보기 목록")
        title.setObjectName("sectionTitle")
        self.vod_count_label = QLabel("0개")
        self.vod_count_label.setObjectName("muted")
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("작업 대상", "work")
        self.filter_combo.addItem("신규만", "new")
        self.filter_combo.addItem("전체", "all")
        self.filter_combo.currentIndexChanged.connect(self.load_vods)
        clear_button = QPushButton("선택 해제")
        clear_button.clicked.connect(self.clear_checks)
        controls.addWidget(title)
        controls.addWidget(self.vod_count_label)
        controls.addStretch(1)
        controls.addWidget(self.filter_combo)
        controls.addWidget(clear_button)
        layout.addLayout(controls)

        self.vod_table = QTableWidget(0, 7)
        self.vod_table.setHorizontalHeaderLabels(
            ["선택", "상태", "스트리머", "영상 제목", "길이", "업로드", "영상/세션 번호"]
        )
        self.vod_table.setAlternatingRowColors(True)
        self.vod_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.vod_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.vod_table.setSortingEnabled(False)
        self.vod_table.verticalHeader().setVisible(False)
        self.vod_table.verticalHeader().setDefaultSectionSize(43)
        self.vod_table.cellDoubleClicked.connect(self.open_vod_from_row)
        self.vod_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.vod_table.customContextMenuRequested.connect(self._show_vod_context_menu)

        header = self.vod_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.vod_table.setColumnWidth(0, 54)
        layout.addWidget(self.vod_table, 1)
        return panel

    def load_streamers(self) -> None:
        self.streamer_list.clear()
        for streamer in self.database.list_streamers(enabled_only=True):
            text = f"{streamer.display_name}\n@{streamer.channel_id}"
            if streamer.last_error:
                text += "  ·  확인 오류"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, streamer.id)
            tooltip_parts = []
            if streamer.glossary:
                tooltip_parts.append(f"단어 사전:\n{streamer.glossary}")
            if streamer.last_error:
                tooltip_parts.append(f"최근 확인 오류:\n{streamer.last_error}")
            item.setToolTip("\n\n".join(tooltip_parts))
            self.streamer_list.addItem(item)

    def load_vods(self) -> None:
        mode = self.filter_combo.currentData() if hasattr(self, "filter_combo") else "work"
        if mode == "new":
            states = [VodState.NEW.value]
        elif mode == "work":
            states = [
                VodState.NEW.value,
                VodState.QUEUED.value,
                VodState.ANALYZING.value,
                VodState.REVIEW.value,
                VodState.READY.value,
                VodState.COPIED.value,
                VodState.FAILED.value,
            ]
        else:
            states = None

        vods = self.database.list_vods(states=states)
        self.vod_table.setRowCount(0)
        for vod in vods:
            row = self.vod_table.rowCount()
            self.vod_table.insertRow(row)

            check_item = QTableWidgetItem()
            check_item.setFlags(
                check_item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
            )
            check_item.setCheckState(Qt.CheckState.Unchecked)
            check_item.setData(Qt.ItemDataRole.UserRole, vod.vod_id)
            check_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.vod_table.setItem(row, 0, check_item)

            values = [
                STATE_LABELS.get(vod.state, vod.state),
                vod.streamer_name,
                vod.title,
                vod.duration_text,
                vod.published_text,
                vod.vod_id,
            ]
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, vod.vod_id)
                if column in (1, 4, 5, 6):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.vod_table.setItem(row, column, item)

        self.vod_count_label.setText(f"{len(vods):,}개")

    def add_streamer(self) -> None:
        try:
            channel_id = normalize_channel_id(self.channel_input.text())
        except ValueError as error:
            QMessageBox.information(self, "입력 확인", str(error))
            return

        self.database.add_streamer(channel_id, self.name_input.text())
        self.channel_input.clear()
        self.name_input.clear()
        self.load_streamers()
        self.status_label.setText(f"@{channel_id}을(를) 추가했습니다.")
        QTimer.singleShot(100, self.refresh_discovery)

    def remove_streamer(self) -> None:
        current = self.streamer_list.currentItem()
        if current is None:
            QMessageBox.information(self, "선택 필요", "삭제할 스트리머를 선택하세요.")
            return
        streamer_id = int(current.data(Qt.ItemDataRole.UserRole))
        vod_ids = self.database.list_vod_ids_for_streamer(streamer_id)
        active_vod_ids = (
            set(self._analysis_jobs)
            | set(self._analysis_queue)
            | set(self._live_jobs)
            | set(self._style_jobs)
            | set(self._regroup_jobs)
        )
        if active_vod_ids.intersection(vod_ids):
            QMessageBox.information(
                self,
                "AI 작업 진행 중",
                "이 스트리머의 AI 작업이 끝난 뒤 삭제하세요.",
            )
            return
        answer = QMessageBox.question(
            self,
            "스트리머 삭제",
            "이 스트리머와 저장된 VOD·타임라인 기록, 로컬 자막 캐시를 삭제할까요?\n"
            "삭제한 기록과 캐시는 프로그램에서 복구할 수 없습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for vod_id in vod_ids:
            editor = self._editor_tabs.pop(vod_id, None)
            if editor is not None:
                editor.blockSignals(True)
                editor.close()
                index = self.tabs.indexOf(editor)
                if index >= 0:
                    self.tabs.removeTab(index)
                editor.deleteLater()
        self.database.remove_streamer(streamer_id)
        removed_caches = sum(1 for vod_id in vod_ids if remove_vod_cache(vod_id))
        self.load_streamers()
        self.load_vods()
        self.status_label.setText(
            f"스트리머 기록과 자막 캐시 {removed_caches:,}개를 삭제했습니다."
        )

    def edit_streamer_glossary(self) -> None:
        current = self.streamer_list.currentItem()
        if current is None:
            QMessageBox.information(self, "선택 필요", "단어 사전을 편집할 스트리머를 선택하세요.")
            return
        streamer_id = int(current.data(Qt.ItemDataRole.UserRole))
        streamer = next(
            (
                item
                for item in self.database.list_streamers()
                if item.id == streamer_id
            ),
            None,
        )
        if streamer is None:
            return
        text, accepted = QInputDialog.getMultiLineText(
            self,
            f"{streamer.display_name} 단어 사전",
            "인명·게임명·고유명사를 한 줄에 하나씩 입력하세요.\n"
            "예: 마이곰이\n월드 오브 워크래프트\n약칭 = 정식 표기",
            streamer.glossary,
        )
        if not accepted:
            return
        if len(text.strip()) > 5_000:
            QMessageBox.information(
                self,
                "단어 사전 길이 초과",
                "Gemini 사용량을 과도하게 늘리지 않도록 단어 사전은 5,000자까지 저장할 수 있습니다.",
            )
            return
        self.database.update_streamer_glossary(streamer_id, text)
        self.load_streamers()
        self.status_label.setText(f"{streamer.display_name} 단어 사전을 저장했습니다.")

    def resolve_manual_link(self) -> None:
        if self._manual_link_job is not None:
            self.status_label.setText("이미 수동 링크를 확인하고 있습니다.")
            return
        value = self.manual_link_input.text().strip()
        try:
            parsed = parse_soop_link(value)
        except ValueError as error:
            QMessageBox.information(self, "링크 확인", str(error))
            return

        if parsed.kind == "live":
            if (
                self._analysis_jobs
                or self._analysis_queue
                or self._style_jobs
                or self._live_jobs
                or self._regroup_jobs
            ):
                QMessageBox.information(
                    self,
                    "AI 작업 진행 중",
                    "라이브 실시간 분석은 다른 AI 작업이 없을 때 시작할 수 있습니다.",
                )
                return
            self.analyzer = LocalWhisperGeminiAnalyzer.from_database(self.database)
            if not self.analyzer.available:
                QMessageBox.information(
                    self,
                    "AI 설정 필요",
                    self.analyzer.unavailable_reason,
                )
                self.open_analysis_settings()
                self.analyzer = LocalWhisperGeminiAnalyzer.from_database(self.database)
                if not self.analyzer.available:
                    return

        thread = QThread(self)
        worker = ManualLinkWorker(value)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.resolved.connect(self._manual_link_resolved)
        worker.failed.connect(self._manual_link_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._manual_link_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._manual_link_job = (thread, worker)
        self.manual_link_button.setEnabled(False)
        self.manual_link_input.setEnabled(False)
        self.status_label.setText(
            "SOOP 라이브 정보와 경과시간 확인 중…"
            if parsed.kind == "live"
            else "SOOP 다시보기 정보 확인 중…"
        )
        thread.start()

    @Slot(object)
    def _manual_link_resolved(self, result: object) -> None:
        if isinstance(result, ResolvedVodLink):
            vod = self.database.upsert_external_vod(
                vod_id=result.vod_id,
                channel_id=result.channel_id,
                streamer_name=result.streamer_name,
                title=result.title,
                url=result.page_url,
                duration_text=result.duration_text,
                published_text=result.published_text,
                thumbnail_url=result.thumbnail_url,
                source_kind="manual_vod",
            )
            self.manual_link_input.clear()
            self.load_vods()
            self.open_timeline(vod.vod_id)
            self.status_label.setText(
                "수동 다시보기를 추가했습니다. 고속 AI 분석을 시작합니다."
            )
            QTimer.singleShot(0, lambda: self.start_analysis(vod.vod_id))
            return

        if isinstance(result, LiveAudioSource):
            now = datetime.now()
            session_id = (
                f"live-{result.broadcast_no}-{now.strftime('%Y%m%d%H%M%S%f')}"
            )
            vod = self.database.upsert_external_vod(
                vod_id=session_id,
                channel_id=result.channel_id,
                streamer_name=result.streamer_name,
                title=f"[LIVE] {result.title}",
                url=result.page_url,
                duration_text=f"시작 {format_timestamp(result.runtime_seconds)}",
                published_text=now.strftime("%Y-%m-%d %H:%M"),
                source_kind="live",
                state=VodState.ANALYZING.value,
            )
            self.manual_link_input.clear()
            self.load_vods()
            self.open_timeline(vod.vod_id)
            self.start_live_analysis(vod.vod_id, result)
            return

        self._manual_link_failed("지원하지 않는 링크 확인 결과입니다.")

    @Slot(str)
    def _manual_link_failed(self, message: str) -> None:
        self.status_label.setText(f"수동 링크 확인 실패: {message}")
        QMessageBox.critical(self, "수동 링크 확인 실패", message)

    @Slot()
    def _manual_link_thread_finished(self) -> None:
        self._manual_link_job = None
        self.manual_link_button.setEnabled(True)
        self.manual_link_input.setEnabled(True)
        if self._close_after_analysis and not self._active_jobs():
            QTimer.singleShot(0, self.close)

    def refresh_discovery(self) -> None:
        if self.discovery.busy:
            self.status_label.setText("이미 신규 영상을 확인하고 있습니다.")
            return
        streamers = self.database.list_streamers(enabled_only=True)
        if not streamers:
            self.status_label.setText("먼저 자동 확인할 스트리머를 추가하세요.")
            return
        self._actual_new_count = 0
        self.discovery.refresh(streamers)

    def _initial_refresh(self) -> None:
        if normalized_discovery_interval(
            self.database.get_setting(DISCOVERY_INTERVAL_SETTING, "180")
        ) > 0 and self.database.list_streamers(enabled_only=True):
            self.refresh_discovery()

    def _on_discovery_started(self, count: int) -> None:
        self.refresh_button.setEnabled(False)
        self.status_label.setText(f"스트리머 {count}명의 신규 영상을 확인합니다…")

    def _on_discovery_result(self, streamer_id: int, streamer_name: str, items: object) -> None:
        if streamer_name:
            self.database.update_streamer_name(streamer_id, streamer_name)
        if isinstance(items, list):
            self._actual_new_count += self.database.upsert_discovered_vods(streamer_id, items)
        self.database.record_discovery_success(streamer_id)

    def _on_discovery_error(self, streamer_id: int, message: str) -> None:
        self.database.record_discovery_error(streamer_id, message)

    def _on_discovery_finished(self, discovered_count: int, error_count: int) -> None:
        del discovered_count
        self.refresh_button.setEnabled(True)
        self.load_streamers()
        self.load_vods()
        checked_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.last_checked_label.setText(f"마지막 확인: {checked_at}")
        if error_count:
            self.status_label.setText(
                f"신규 {self._actual_new_count}개 · 확인 오류 {error_count}명"
            )
        else:
            self.status_label.setText(f"신규 영상 {self._actual_new_count}개를 추가했습니다.")
        if self._actual_new_count > 0 and setting_enabled(
            self.database.get_setting(NEW_VOD_NOTIFICATION_SETTING, "1")
        ):
            if self.tray_icon is not None:
                self.tray_icon.showMessage(
                    "SOOP 신규 다시보기",
                    f"자동 확인 목록에서 새 영상 {self._actual_new_count:,}개를 찾았습니다.",
                    QSystemTrayIcon.MessageIcon.Information,
                    6_000,
                )
            else:
                QApplication.beep()

    def selected_vod_ids(self) -> list[str]:
        selected: list[str] = []
        for row in range(self.vod_table.rowCount()):
            item = self.vod_table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                selected.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return selected

    def clear_checks(self) -> None:
        for row in range(self.vod_table.rowCount()):
            item = self.vod_table.item(row, 0)
            if item:
                item.setCheckState(Qt.CheckState.Unchecked)

    def open_selected_timelines(self) -> None:
        vod_ids = self.selected_vod_ids()
        if not vod_ids:
            QMessageBox.information(self, "선택 필요", "타임라인을 작성할 영상을 선택하세요.")
            return
        for vod_id in vod_ids:
            self.open_timeline(vod_id)
        for vod_id in vod_ids:
            vod = self.database.get_vod(vod_id)
            if vod is not None and vod.source_kind != "live":
                self.start_analysis(vod_id)
        self.clear_checks()
        self.load_vods()

    def open_timeline(self, vod_id: str) -> None:
        existing_editor = self._editor_tabs.get(vod_id)
        if existing_editor is not None:
            self.tabs.setCurrentWidget(existing_editor)
            return

        vod = self.database.get_vod(vod_id)
        if vod is None:
            return
        document = self.database.get_timeline(vod_id)
        text = document.text if document else self.analyzer.initial_document(vod)
        if document is None:
            self.database.save_timeline(vod_id, text, VodState.REVIEW.value)
        self.database.set_vod_state(vod_id, VodState.REVIEW.value)

        editor = TimelineDocumentEditor(
            vod,
            text,
            self.analyzer.available,
            self.analyzer.unavailable_reason,
            self.styler.available,
            self.styler.unavailable_reason,
        )
        editor.document_changed.connect(self._save_timeline)
        editor.review_completed.connect(self._mark_review_complete)
        editor.analysis_requested.connect(self.start_analysis)
        editor.analysis_cancel_requested.connect(self.cancel_analysis)
        editor.reanalyze_as_vod_requested.connect(self.reanalyze_live_as_vod)
        editor.style_requested.connect(self.start_style_correction)
        editor.line_rewrite_requested.connect(self.start_line_rewrite)
        editor.regroup_requested.connect(self.start_topic_regroup)
        editor.snapshot_requested.connect(self._snapshot_timeline)
        editor.version_history_requested.connect(self.show_version_history)
        editor.transcript_requested.connect(self.show_cached_transcript)
        editor.cache_delete_requested.connect(self.delete_vod_cache)
        self._editor_tabs[vod_id] = editor
        self._refresh_editor_cache_state(vod_id)
        title = vod.title if len(vod.title) <= 22 else f"{vod.title[:21]}…"
        index = self.tabs.addTab(editor, title)
        self.tabs.setTabToolTip(index, vod.title)
        self.tabs.setCurrentIndex(index)

    def _refresh_editor_cache_state(self, vod_id: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        vod = self.database.get_vod(vod_id)
        if editor is None or vod is None:
            return
        transcript_available = load_cached_transcript(vod) is not None
        editor.set_cached_transcript_available(
            transcript_available,
            has_vod_cache(vod_id),
        )
        editor.set_final_pending(has_pending_timeline_finalization(vod_id))

    def _refresh_all_editor_cache_states(self) -> None:
        for vod_id in list(self._editor_tabs):
            self._refresh_editor_cache_state(vod_id)

    @Slot(str)
    def show_cached_transcript(self, vod_id: str) -> None:
        vod = self.database.get_vod(vod_id)
        if vod is None:
            return
        transcript = load_cached_transcript(vod)
        if transcript is None:
            QMessageBox.information(
                self,
                "저장 자막 없음",
                "완료된 Whisper 자막이 없습니다. 분석을 시작하거나 완료한 뒤 다시 확인하세요.",
            )
            self._refresh_editor_cache_state(vod_id)
            return
        TranscriptViewerDialog(vod, transcript, self).exec()

    @Slot(str)
    def delete_vod_cache(self, vod_id: str) -> None:
        if (
            vod_id in self._analysis_jobs
            or vod_id in self._analysis_queue
            or vod_id in self._live_jobs
            or vod_id in self._regroup_jobs
        ):
            QMessageBox.information(
                self,
                "AI 작업 진행 중",
                "분석 또는 자막 재정리가 끝난 뒤 캐시를 삭제하세요.",
            )
            return
        answer = QMessageBox.question(
            self,
            "이 영상 자막 캐시 삭제",
            "Whisper 자막과 AI 중간 결과를 삭제할까요?\n"
            "현재 타임라인 문서는 유지됩니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = remove_vod_cache(vod_id)
        self._refresh_editor_cache_state(vod_id)
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.status_label.setText(
                "자막 캐시를 삭제했습니다."
                if removed
                else "삭제할 자막 캐시가 없습니다."
            )

    def _save_timeline(self, vod_id: str, text: str) -> None:
        if vod_id in self._live_jobs:
            self.database.save_timeline(
                vod_id,
                text,
                VodState.ANALYZING.value,
            )
            return
        self.database.save_timeline(vod_id, text, VodState.REVIEW.value)
        vod = self.database.get_vod(vod_id)
        if vod and vod.state != VodState.READY.value:
            self.database.set_vod_state(vod_id, VodState.REVIEW.value)

    @Slot(str, str, str)
    def _snapshot_timeline(self, vod_id: str, reason: str, text: str) -> None:
        self.database.create_timeline_revision(vod_id, text, reason)

    @Slot(str)
    def show_version_history(self, vod_id: str) -> None:
        revisions = self.database.list_timeline_revisions(vod_id)
        if not revisions:
            QMessageBox.information(
                self,
                "버전 기록 없음",
                "아직 저장된 이전 버전이 없습니다. AI 재분석·주제 재묶기·일괄 변경 전에 자동 생성됩니다.",
            )
            return
        dialog = TimelineVersionHistoryDialog(revisions, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.restored_text is None:
            return
        editor = self._editor_tabs.get(vod_id)
        if editor is None:
            return
        self.database.create_timeline_revision(
            vod_id,
            editor.text(),
            "버전 복원 전",
        )
        editor.set_text(dialog.restored_text)
        self.database.save_timeline(vod_id, dialog.restored_text, VodState.REVIEW.value)
        editor.status_label.setText("선택한 버전으로 복원했습니다.")

    def _mark_review_complete(self, vod_id: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            self.database.save_timeline(vod_id, editor.text(), VodState.READY.value)
        self.database.set_vod_state(vod_id, VodState.READY.value)
        self.load_vods()

    def open_analysis_settings(self) -> None:
        dialog = AnalysisSettingsDialog(
            self.database,
            self,
            cache_actions_enabled=not self._active_jobs(),
        )
        result = dialog.exec()
        self._refresh_all_editor_cache_states()
        if result != QDialog.DialogCode.Accepted:
            return
        self.analyzer = LocalWhisperGeminiAnalyzer.from_database(self.database)
        self.styler = AITimelineStyler.from_database(self.database)
        self._configure_refresh_timer()
        for editor in self._editor_tabs.values():
            editor.set_analyzer_availability(
                self.analyzer.available,
                self.analyzer.unavailable_reason,
            )
            editor.set_style_availability(
                self.styler.available,
                self.styler.unavailable_reason,
            )
        if self.analyzer.available:
            self.status_label.setText("AI 분석 설정을 저장했습니다.")
        else:
            self.status_label.setText(self.analyzer.unavailable_reason)

    def check_for_updates(self, *, silent: bool = False) -> None:
        if self._update_reply is not None:
            if not silent:
                self.status_label.setText("업데이트를 확인하고 있습니다…")
            return

        manifest_url = configured_manifest_url(self.database)
        if not manifest_url:
            if not silent:
                QMessageBox.information(
                    self,
                    "업데이트 주소 필요",
                    "아직 업데이트 확인 주소가 설정되지 않았습니다.\n\n"
                    "AI 설정의 ‘앱 업데이트’에서 배포용 update.json 또는 "
                    "GitHub 최신 릴리스 API 주소를 입력하세요.",
                )
            return

        url = QUrl(manifest_url)
        if not url.isValid() or url.scheme().lower() not in {"https", "http"}:
            if not silent:
                QMessageBox.warning(
                    self,
                    "업데이트 주소 오류",
                    "업데이트 확인 주소는 http 또는 https 주소여야 합니다.",
                )
            return

        request = QNetworkRequest(url)
        request.setHeader(
            QNetworkRequest.KnownHeaders.UserAgentHeader,
            f"SOOPTimeline/{__version__}",
        )
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        self._update_check_silent = silent
        self._update_reply = self.update_network.get(request)
        self._update_reply.finished.connect(self._finish_update_check)
        self.update_timeout.start()
        self.update_button.setEnabled(False)
        if not silent:
            self.status_label.setText("새 버전을 확인하고 있습니다…")

    @Slot()
    def _abort_update_check(self) -> None:
        if self._update_reply is not None and self._update_reply.isRunning():
            self._update_reply.abort()

    @Slot()
    def _finish_update_check(self) -> None:
        reply = self._update_reply
        if reply is None:
            return
        silent = self._update_check_silent
        self._update_reply = None
        self.update_timeout.stop()
        self.update_button.setEnabled(True)

        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                raise RuntimeError(reply.errorString())
            info = parse_update_manifest(bytes(reply.readAll()), __version__)
        except Exception as error:
            if not silent:
                QMessageBox.warning(
                    self,
                    "업데이트 확인 실패",
                    f"업데이트 정보를 확인하지 못했습니다.\n\n{error}",
                )
                self.status_label.setText("업데이트 확인에 실패했습니다.")
            reply.deleteLater()
            return

        reply.deleteLater()
        if not info.update_available:
            if not silent:
                QMessageBox.information(
                    self,
                    "최신 버전",
                    f"현재 v{__version__}이 최신 버전입니다.",
                )
                self.status_label.setText(f"최신 버전 v{__version__} 사용 중")
            return

        self.status_label.setText(
            f"새 버전 v{info.latest_version}을 사용할 수 있습니다."
        )
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Information)
        message.setWindowTitle("새 업데이트 발견")
        message.setText(
            f"SOOP AI 타임라인 v{info.latest_version}이 나왔습니다.\n"
            f"현재 버전은 v{__version__}입니다."
        )
        if info.release_notes:
            notes = info.release_notes[:1_500]
            if len(info.release_notes) > len(notes):
                notes += "…"
            message.setInformativeText(notes)
        if info.download_url:
            open_button = message.addButton(
                "다운로드 페이지 열기",
                QMessageBox.ButtonRole.AcceptRole,
            )
        else:
            open_button = None
            message.setInformativeText(
                (message.informativeText() + "\n\n" if message.informativeText() else "")
                + "배포 정보에 다운로드 주소가 없습니다."
            )
        message.addButton("나중에", QMessageBox.ButtonRole.RejectRole)
        message.exec()
        if open_button is not None and message.clickedButton() is open_button:
            QDesktopServices.openUrl(QUrl(info.download_url))

    def _resume_persisted_analysis(self) -> None:
        if self._analysis_jobs or not self._analysis_queue:
            return
        vod_id = self._analysis_queue.pop(0)
        if self.database.get_vod(vod_id) is None:
            self.database.remove_analysis_queue(vod_id)
            QTimer.singleShot(0, self._resume_persisted_analysis)
            return
        self.open_timeline(vod_id)
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.status_label.setText(
                "이전 실행에서 중단된 분석을 체크포인트부터 재개합니다…"
            )
        self.start_analysis(vod_id, _from_queue=True)

    def reanalyze_live_as_vod(self, live_vod_id: str) -> None:
        """Analyze the finished broadcast's full replay VOD from scratch.

        The live session only captured audio from the join time onward, so a
        completed replay is re-run through the normal manual-link → VOD path
        (real vod_id, working review player, full-length transcript).
        """
        if self._active_jobs():
            QMessageBox.information(
                self,
                "AI 작업 진행 중",
                "다른 AI 작업이 끝난 뒤 다시보기 전체 분석을 시작하세요.",
            )
            return
        if self.database.get_vod(live_vod_id) is None:
            return
        link, accepted = QInputDialog.getText(
            self,
            "다시보기 전체로 재분석",
            "방송이 끝난 뒤 올라온 ‘다시보기’ 영상 주소를 붙여넣으세요.\n"
            "라이브 페이지가 아니라 vod.sooplive.com 다시보기 주소여야 전체가 분석됩니다.\n"
            "예: https://vod.sooplive.com/player/00000000",
        )
        if not accepted:
            return
        link = link.strip()
        if not link:
            return
        self.manual_link_input.setText(link)
        self.resolve_manual_link()

    def start_analysis(self, vod_id: str, *, _from_queue: bool = False) -> None:
        vod = self.database.get_vod(vod_id)
        if vod is not None and vod.source_kind == "live":
            editor = self._editor_tabs.get(vod_id)
            if editor is not None:
                editor.status_label.setText(
                    "라이브 세션은 수동 링크 입력창에 방송 링크를 다시 넣어 시작하세요."
                )
            return
        if self._live_jobs:
            editor = self._editor_tabs.get(vod_id)
            if editor is not None:
                editor.status_label.setText(
                    "라이브 실시간 분석이 끝난 뒤 다시보기 분석을 시작할 수 있습니다."
                )
            return
        if self._style_jobs:
            editor = self._editor_tabs.get(vod_id)
            if editor is not None:
                editor.status_label.setText("AI 문체 교정이 끝난 뒤 분석할 수 있습니다.")
            return
        if self._regroup_jobs:
            editor = self._editor_tabs.get(vod_id)
            if editor is not None:
                editor.status_label.setText("주제 다시 묶기가 끝난 뒤 분석할 수 있습니다.")
            return
        if vod_id in self._analysis_jobs or (
            vod_id in self._analysis_queue and not _from_queue
        ):
            editor = self._editor_tabs.get(vod_id)
            if editor is not None:
                editor.status_label.setText("이미 분석 중이거나 대기열에 있습니다.")
            return

        if self._analysis_jobs:
            running_vod_id = next(iter(self._analysis_jobs))
            running_vod = self.database.get_vod(running_vod_id)
            running_title = running_vod.title if running_vod else running_vod_id
            self._analysis_queue.append(vod_id)
            self.database.enqueue_analysis(vod_id)
            self.database.set_vod_state(vod_id, VodState.QUEUED.value)
            editor = self._editor_tabs.get(vod_id)
            if editor is not None:
                editor.status_label.setText(
                    f"분석 대기 중 · 현재 작업: {running_title}"
                )
            self.status_label.setText(
                f"분석 대기열에 추가했습니다 · 대기 {len(self._analysis_queue)}개"
            )
            self.load_vods()
            return

        self.analyzer = LocalWhisperGeminiAnalyzer.from_database(self.database)
        if not self.analyzer.available:
            QMessageBox.information(
                self,
                "AI 설정 필요",
                self.analyzer.unavailable_reason,
            )
            self.open_analysis_settings()
            self.analyzer = LocalWhisperGeminiAnalyzer.from_database(self.database)
            if not self.analyzer.available:
                return

        vod = self.database.get_vod(vod_id)
        editor = self._editor_tabs.get(vod_id)
        if vod is None or editor is None:
            return

        self.database.create_timeline_revision(
            vod_id,
            editor.text(),
            "AI 분석 전",
        )

        thread = QThread(self)
        thread.setProperty("vod_id", vod_id)
        worker = AnalysisWorker(self.analyzer, vod)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress_changed.connect(editor.set_analysis_progress)
        worker.preview_changed.connect(editor.set_analysis_preview)
        worker.usage_changed.connect(editor.set_ai_usage)
        worker.succeeded.connect(self._analysis_succeeded)
        worker.failed.connect(self._analysis_failed)
        worker.cancelled.connect(self._analysis_cancelled)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._analysis_thread_finished)
        thread.finished.connect(thread.deleteLater)

        self._analysis_jobs[vod_id] = (thread, worker)
        self.database.mark_analysis_running(vod_id)
        self.database.set_vod_state(vod_id, VodState.ANALYZING.value)
        editor.set_analysis_running(True)
        duration_seconds = parse_duration_text(vod.duration_text)
        estimate = estimate_timeline_calls(duration_seconds or 0)
        editor.set_analysis_progress(
            0,
            f"SOOP 고속 오디오 분석 준비 · AI 호출 예상 약 {estimate:,}회 "
            "(자막 구간 수에 따라 달라질 수 있음)",
        )
        self.status_label.setText(f"AI 분석 시작: {vod.title}")
        self.load_vods()
        thread.start()

    def start_live_analysis(
        self,
        vod_id: str,
        source: LiveAudioSource,
    ) -> None:
        if (
            self._analysis_jobs
            or self._analysis_queue
            or self._style_jobs
            or self._live_jobs
            or self._regroup_jobs
        ):
            self._manual_link_failed(
                "다른 AI 작업이 진행 중이어서 라이브 분석을 시작하지 못했습니다."
            )
            return
        vod = self.database.get_vod(vod_id)
        editor = self._editor_tabs.get(vod_id)
        analyzer = LocalWhisperGeminiAnalyzer.from_database(self.database)
        if vod is None or editor is None:
            return
        if not analyzer.available:
            self._manual_link_failed(analyzer.unavailable_reason)
            return

        thread = QThread(self)
        thread.setProperty("vod_id", vod_id)
        worker = LiveAnalysisWorker(analyzer, vod, source)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress_changed.connect(self._live_progress_changed)
        worker.preview_changed.connect(self._live_preview_changed)
        worker.usage_changed.connect(self._ai_usage_changed)
        worker.succeeded.connect(self._live_succeeded)
        worker.failed.connect(self._live_failed)
        worker.cancelled.connect(self._live_cancelled)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._live_thread_finished)
        thread.finished.connect(thread.deleteLater)

        self._live_jobs[vod_id] = (thread, worker)
        self.database.set_vod_state(vod_id, VodState.ANALYZING.value)
        editor.set_live_running(True)
        editor.set_analysis_progress(
            0,
            "라이브 연결 완료 · 방송 "
            f"{format_timestamp(source.runtime_seconds)}부터 실시간 분석을 시작합니다…",
        )
        self.status_label.setText(
            f"라이브 실시간 분석 시작: {source.streamer_name} · "
            f"{format_timestamp(source.runtime_seconds)}"
        )
        self.load_vods()
        thread.start()

    @Slot(str, int, str)
    def _live_progress_changed(
        self,
        vod_id: str,
        percent: int,
        message: str,
    ) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_analysis_progress(percent, message)

    @Slot(str, str, str)
    def _live_preview_changed(
        self,
        vod_id: str,
        stage: str,
        text: str,
    ) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is None:
            return
        editor.apply_live_update(stage, text)
        if stage == "live_timeline":
            self.database.save_timeline(
                vod_id,
                text,
                VodState.ANALYZING.value,
            )

    @Slot(str, str)
    def _live_succeeded(self, vod_id: str, document: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.apply_live_result(document)
        self.database.save_timeline(vod_id, document, VodState.REVIEW.value)
        self.database.set_vod_state(vod_id, VodState.REVIEW.value)
        self.status_label.setText(
            "라이브 수신과 AI 최종 타임라인 정리가 완료되었습니다."
        )
        self._refresh_editor_cache_state(vod_id)
        self.load_vods()

    @Slot(str, str)
    def _live_failed(self, vod_id: str, message: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_live_running(False)
            self.database.save_timeline(
                vod_id,
                editor.text(),
                VodState.FAILED.value,
            )
            editor.status_label.setText(f"라이브 분석 실패: {message}")
        self.database.set_vod_state(vod_id, VodState.FAILED.value)
        self._refresh_editor_cache_state(vod_id)
        self.status_label.setText("라이브 실시간 분석에 실패했습니다.")
        self.load_vods()
        QMessageBox.critical(self, "라이브 분석 실패", message)

    @Slot(str)
    def _live_cancelled(self, vod_id: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_live_running(False)
            editor.status_label.setText("라이브 분석을 중단했습니다.")
        self.database.set_vod_state(vod_id, VodState.REVIEW.value)
        self._refresh_editor_cache_state(vod_id)
        self.load_vods()

    @Slot()
    def _live_thread_finished(self) -> None:
        thread = self.sender()
        vod_id = str(thread.property("vod_id") or "") if thread is not None else ""
        if vod_id:
            self._live_jobs.pop(vod_id, None)
        if self._close_after_analysis and not self._active_jobs():
            QTimer.singleShot(0, self.close)

    def start_style_correction(self, vod_id: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is None:
            return
        if self._analysis_jobs or self._analysis_queue or self._live_jobs:
            editor.status_label.setText("영상 분석이 끝난 뒤 문체를 교정할 수 있습니다.")
            return
        if self._regroup_jobs:
            editor.status_label.setText("주제 다시 묶기가 끝난 뒤 문체를 교정할 수 있습니다.")
            return
        if self._style_jobs:
            editor.status_label.setText("이미 AI 문체 교정 작업이 진행 중입니다.")
            return

        self.styler = AITimelineStyler.from_database(self.database)
        if not self.styler.available:
            QMessageBox.information(
                self,
                "AI 설정 필요",
                self.styler.unavailable_reason,
            )
            self.open_analysis_settings()
            self.styler = AITimelineStyler.from_database(self.database)
            if not self.styler.available:
                return

        document = editor.text()
        if not document.strip():
            QMessageBox.information(self, "내용 없음", "교정할 타임라인이 없습니다.")
            return
        self.database.create_timeline_revision(
            vod_id,
            document,
            "AI 문체 교정 전",
        )

        self._save_timeline(vod_id, document)
        thread = QThread(self)
        thread.setProperty("vod_id", vod_id)
        worker = TimelineStyleWorker(self.styler, vod_id, document)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._style_succeeded)
        worker.usage_changed.connect(self._ai_usage_changed)
        worker.failed.connect(self._style_failed)
        worker.cancelled.connect(self._style_cancelled)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._style_thread_finished)
        thread.finished.connect(thread.deleteLater)

        self._style_jobs[vod_id] = (thread, worker)
        editor.set_style_running(True)
        self.status_label.setText("AI 문체 교정을 시작했습니다.")
        thread.start()

    @Slot(str, str)
    def _style_succeeded(self, vod_id: str, document: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.apply_style_result(document)
            editor.set_style_running(False)
        self.database.save_timeline(vod_id, document, VodState.REVIEW.value)
        self.database.set_vod_state(vod_id, VodState.REVIEW.value)
        self.status_label.setText("AI 문체 교정이 완료되었습니다.")
        self.load_vods()

    @Slot(str, str)
    def _style_failed(self, vod_id: str, message: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_style_running(False)
            editor.status_label.setText(f"문체 교정 실패: {message}")
        self.status_label.setText("AI 문체 교정에 실패했습니다.")
        QMessageBox.critical(self, "AI 문체 교정 실패", message)

    @Slot(str)
    def _style_cancelled(self, vod_id: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_style_running(False)
            editor.status_label.setText("AI 문체 교정을 취소했습니다.")

    @Slot()
    def _style_thread_finished(self) -> None:
        thread = self.sender()
        vod_id = str(thread.property("vod_id") or "") if thread is not None else ""
        if not vod_id:
            return
        self._style_jobs.pop(vod_id, None)

    @Slot(str, str, str, int)
    def start_line_rewrite(
        self,
        vod_id: str,
        mode: str,
        line: str,
        next_seconds: int,
    ) -> None:
        editor = self._editor_tabs.get(vod_id)
        vod = self.database.get_vod(vod_id)
        if editor is None or vod is None:
            return
        if (
            vod_id in self._analysis_jobs
            or vod_id in self._analysis_queue
            or vod_id in self._live_jobs
            or vod_id in self._regroup_jobs
            or vod_id in self._style_jobs
        ):
            editor.status_label.setText(
                "진행 중인 AI 작업이 끝난 뒤 줄 변환을 사용할 수 있습니다."
            )
            return
        if vod_id in self._line_rewrite_jobs:
            editor.status_label.setText("이미 이 탭에서 줄 변환이 진행 중입니다.")
            return

        rewriter = AITimelineLineRewriter.from_database(self.database)
        if not rewriter.available:
            QMessageBox.information(
                self,
                "AI 설정 필요",
                rewriter.unavailable_reason,
            )
            self.open_analysis_settings()
            rewriter = AITimelineLineRewriter.from_database(self.database)
            if not rewriter.available:
                return

        transcript = load_cached_transcript(vod)
        if transcript is None:
            editor.status_label.setText(
                "저장 자막이 없어 줄을 변환할 수 없습니다. 먼저 AI 분석을 실행하세요."
            )
            return

        thread = QThread(self)
        thread.setProperty("vod_id", vod_id)
        worker = TimelineLineRewriteWorker(
            rewriter,
            vod_id,
            mode,
            line,
            next_seconds,
            transcript,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._line_rewrite_succeeded)
        worker.usage_changed.connect(self._ai_usage_changed)
        worker.failed.connect(self._line_rewrite_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._line_rewrite_thread_finished)
        thread.finished.connect(thread.deleteLater)

        self._line_rewrite_jobs[vod_id] = (thread, worker)
        editor.set_line_rewrite_running(True, mode)
        thread.start()

    @Slot(str, str, str)
    def _line_rewrite_succeeded(
        self,
        vod_id: str,
        original_line: str,
        new_line: str,
    ) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is None:
            return
        editor.set_line_rewrite_running(False)
        editor.apply_line_rewrite(original_line, new_line)

    @Slot(str, str)
    def _line_rewrite_failed(self, vod_id: str, message: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_line_rewrite_running(False)
            editor.status_label.setText(f"줄 변환 실패: {message}")

    @Slot()
    def _line_rewrite_thread_finished(self) -> None:
        thread = self.sender()
        vod_id = str(thread.property("vod_id") or "") if thread is not None else ""
        if not vod_id:
            return
        self._line_rewrite_jobs.pop(vod_id, None)

    @Slot(str, str)
    def start_topic_regroup(self, vod_id: str, granularity: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        vod = self.database.get_vod(vod_id)
        if editor is None or vod is None:
            return
        if (
            self._analysis_jobs
            or self._analysis_queue
            or self._live_jobs
            or self._style_jobs
            or self._regroup_jobs
        ):
            editor.status_label.setText("다른 AI 작업이 끝난 뒤 주제를 다시 묶을 수 있습니다.")
            return
        analyzer = LocalWhisperGeminiAnalyzer.from_database(self.database)
        if not analyzer.available:
            QMessageBox.information(self, "AI 설정 필요", analyzer.unavailable_reason)
            return

        self.database.create_timeline_revision(
            vod_id,
            editor.text(),
            f"주제 다시 묶기 전 ({granularity})",
        )
        thread = QThread(self)
        thread.setProperty("vod_id", vod_id)
        worker = TimelineRegroupWorker(analyzer, vod, granularity)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress_changed.connect(self._regroup_progress_changed)
        worker.preview_changed.connect(self._regroup_preview_changed)
        worker.usage_changed.connect(self._ai_usage_changed)
        worker.succeeded.connect(self._regroup_succeeded)
        worker.failed.connect(self._regroup_failed)
        worker.cancelled.connect(self._regroup_cancelled)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._regroup_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._regroup_jobs[vod_id] = (thread, worker)
        editor.set_regroup_running(True)
        editor.analysis_progress.setVisible(True)
        editor.set_analysis_progress(0, "저장된 자막으로 주제 다시 묶기를 준비합니다…")
        self.status_label.setText("주제 다시 묶기를 시작했습니다.")
        thread.start()

    @Slot(str, int, str)
    def _regroup_progress_changed(self, vod_id: str, percent: int, message: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_analysis_progress(percent, message)

    @Slot(str, str)
    def _ai_usage_changed(self, vod_id: str, summary: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_ai_usage(summary)

    @Slot(str, str, str)
    def _regroup_preview_changed(self, vod_id: str, stage: str, text: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_analysis_preview(stage, text)

    @Slot(str, str)
    def _regroup_succeeded(self, vod_id: str, document: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.apply_regroup_result(document)
            editor.analysis_progress.setVisible(False)
        self.database.save_timeline(vod_id, document, VodState.REVIEW.value)
        self.database.set_vod_state(vod_id, VodState.REVIEW.value)
        self._refresh_editor_cache_state(vod_id)
        if has_pending_timeline_finalization(vod_id):
            self.status_label.setText(
                "구간별 임시 타임라인을 저장했습니다. Gemini 한도 복구 후 최종 정리를 재시도하세요."
            )
            if editor is not None:
                editor.status_label.setText(self.status_label.text())
        else:
            self.status_label.setText("주제 다시 묶기가 완료되었습니다.")

    @Slot(str, str)
    def _regroup_failed(self, vod_id: str, message: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_regroup_running(False)
            editor.analysis_progress.setVisible(False)
            editor.status_label.setText(f"주제 다시 묶기 실패: {message}")
        self._refresh_editor_cache_state(vod_id)
        QMessageBox.critical(self, "주제 다시 묶기 실패", message)

    @Slot(str)
    def _regroup_cancelled(self, vod_id: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_regroup_running(False)
            editor.analysis_progress.setVisible(False)
            editor.status_label.setText("주제 다시 묶기를 취소했습니다.")
        self._refresh_editor_cache_state(vod_id)

    @Slot()
    def _regroup_thread_finished(self) -> None:
        thread = self.sender()
        vod_id = str(thread.property("vod_id") or "") if thread is not None else ""
        if vod_id:
            self._regroup_jobs.pop(vod_id, None)
        if self._close_after_analysis and not self._active_jobs():
            QTimer.singleShot(0, self.close)

    def cancel_analysis(self, vod_id: str) -> None:
        live_job = self._live_jobs.get(vod_id)
        if live_job is not None:
            thread, _ = live_job
            thread.requestInterruption()
            editor = self._editor_tabs.get(vod_id)
            if editor is not None:
                editor.request_live_stop()
            self.status_label.setText(
                "라이브 종료 요청됨 · 남은 자막과 AI 최종 타임라인을 정리합니다…"
            )
            return
        job = self._analysis_jobs.get(vod_id)
        if job is None:
            return
        thread, _ = job
        thread.requestInterruption()
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_analysis_progress(
                editor.analysis_progress.value(),
                "취소를 요청했습니다. 현재 처리 구간이 끝날 때까지 기다려주세요…",
            )

    @Slot(str, str)
    def _analysis_succeeded(self, vod_id: str, document: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.apply_analysis_result(document)
            editor.set_analysis_running(False)
        self.database.save_timeline(vod_id, document, VodState.REVIEW.value)
        self.database.set_vod_state(vod_id, VodState.REVIEW.value)
        self.database.remove_analysis_queue(vod_id)
        self._refresh_editor_cache_state(vod_id)
        if has_pending_timeline_finalization(vod_id):
            self.status_label.setText(
                "구간별 임시 타임라인을 저장했습니다. Gemini 한도 복구 후 최종 정리를 재시도하세요."
            )
            if editor is not None:
                editor.status_label.setText(self.status_label.text())
        else:
            self.status_label.setText("AI 타임라인 생성이 완료되었습니다. 결과를 검수하세요.")
        self.load_vods()

    @Slot(str, str)
    def _analysis_failed(self, vod_id: str, message: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_analysis_running(False)
            editor.status_label.setText(f"분석 실패: {message}")
        self.database.set_vod_state(vod_id, VodState.FAILED.value)
        self.database.remove_analysis_queue(vod_id)
        self._refresh_editor_cache_state(vod_id)
        self.status_label.setText("AI 분석에 실패했습니다.")
        self.load_vods()
        QMessageBox.critical(self, "AI 분석 실패", message)

    @Slot(str)
    def _analysis_cancelled(self, vod_id: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_analysis_running(False)
            editor.status_label.setText("분석을 취소했습니다.")
        self.database.set_vod_state(vod_id, VodState.REVIEW.value)
        self.database.remove_analysis_queue(vod_id)
        self._refresh_editor_cache_state(vod_id)
        self.status_label.setText("AI 분석을 취소했습니다.")
        self.load_vods()

    @Slot()
    def _analysis_thread_finished(self) -> None:
        thread = self.sender()
        vod_id = str(thread.property("vod_id") or "") if thread is not None else ""
        if not vod_id:
            return
        self._analysis_jobs.pop(vod_id, None)
        if self._close_after_analysis:
            self._analysis_queue.clear()
            if not self._active_jobs():
                QTimer.singleShot(0, self.close)
            return
        if self._analysis_queue:
            next_vod_id = self._analysis_queue.pop(0)
            QTimer.singleShot(
                0,
                lambda: self.start_analysis(next_vod_id, _from_queue=True),
            )

    def _vod_active_job(self, vod_id: str) -> bool:
        return (
            vod_id in self._analysis_jobs
            or vod_id in self._analysis_queue
            or vod_id in self._live_jobs
            or vod_id in self._style_jobs
            or vod_id in self._regroup_jobs
        )

    def _show_vod_context_menu(self, pos) -> None:
        item = self.vod_table.itemAt(pos)
        if item is None:
            return
        id_item = self.vod_table.item(item.row(), 0)
        if id_item is None:
            return
        vod_id = str(id_item.data(Qt.ItemDataRole.UserRole))
        vod = self.database.get_vod(vod_id)
        if vod is None:
            return

        menu = QMenu(self)
        if self._vod_active_job(vod_id):
            cancel_action = menu.addAction("AI 작업 취소")
            cancel_action.triggered.connect(
                lambda _=False, vid=vod_id: self._cancel_or_dequeue(vid)
            )
        delete_action = menu.addAction("목록에서 삭제")
        delete_action.triggered.connect(
            lambda _=False, vid=vod_id: self.delete_vod_entry(vid)
        )
        menu.exec(self.vod_table.viewport().mapToGlobal(pos))

    def _cancel_or_dequeue(self, vod_id: str) -> None:
        if vod_id in self._analysis_jobs or vod_id in self._live_jobs:
            self.cancel_analysis(vod_id)
            return
        if vod_id in self._analysis_queue:
            self._analysis_queue.remove(vod_id)
            self.database.remove_analysis_queue(vod_id)
            self.database.set_vod_state(vod_id, VodState.REVIEW.value)
            editor = self._editor_tabs.get(vod_id)
            if editor is not None:
                editor.status_label.setText("분석 대기를 취소했습니다.")
            self.status_label.setText("분석 대기열에서 제거했습니다.")
            self.load_vods()
            return
        job = self._style_jobs.get(vod_id) or self._regroup_jobs.get(vod_id)
        if job is not None:
            job[0].requestInterruption()
            self.status_label.setText("진행 중인 AI 작업 취소를 요청했습니다…")

    def delete_vod_entry(self, vod_id: str) -> None:
        if self._vod_active_job(vod_id):
            QMessageBox.information(
                self,
                "AI 작업 진행 중",
                "이 항목의 AI 작업을 먼저 취소한 뒤 삭제하세요.",
            )
            return
        vod = self.database.get_vod(vod_id)
        if vod is None:
            return
        answer = QMessageBox.question(
            self,
            "목록에서 삭제",
            f"'{vod.title}' 항목과 저장된 타임라인·이전 버전·자막 캐시를 삭제할까요?\n"
            "SOOP의 원본 영상은 지워지지 않지만, 이 프로그램의 기록은 복구할 수 없습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        editor = self._editor_tabs.pop(vod_id, None)
        if editor is not None:
            editor.blockSignals(True)
            index = self.tabs.indexOf(editor)
            if index >= 0:
                self.tabs.removeTab(index)
            editor.deleteLater()
        if vod_id in self._analysis_queue:
            self._analysis_queue.remove(vod_id)
        self.database.remove_analysis_queue(vod_id)
        remove_vod_cache(vod_id)
        self.database.delete_vod(vod_id)
        self.load_vods()
        self.status_label.setText("항목을 목록에서 삭제했습니다.")

    def open_vod_from_row(self, row: int, column: int) -> None:
        del column
        item = self.vod_table.item(row, 0)
        if item is None:
            return
        vod = self.database.get_vod(str(item.data(Qt.ItemDataRole.UserRole)))
        if vod:
            QDesktopServices.openUrl(QUrl(vod.url))

    def _close_tab(self, index: int) -> None:
        if index == 0:
            return
        widget = self.tabs.widget(index)
        if isinstance(widget, TimelineDocumentEditor):
            if (
                widget.vod.vod_id in self._analysis_jobs
                or widget.vod.vod_id in self._analysis_queue
                or widget.vod.vod_id in self._style_jobs
                or widget.vod.vod_id in self._live_jobs
                or widget.vod.vod_id in self._regroup_jobs
            ):
                QMessageBox.information(
                    self,
                    "분석 작업 중",
                    "AI 작업을 취소하거나 완료한 뒤 탭을 닫으세요.",
                )
                return
            self._save_timeline(widget.vod.vod_id, widget.text())
            self._editor_tabs.pop(widget.vod.vod_id, None)
        self.tabs.removeTab(index)
        widget.deleteLater()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._active_jobs():
            event.accept()
            return
        answer = QMessageBox.question(
            self,
            "AI 작업 진행 중",
            "진행 중인 AI 작업을 취소하고 프로그램을 종료할까요?\n"
            "현재 API 요청 또는 분석 구간이 끝날 때까지 잠시 걸릴 수 있습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            event.ignore()
            return
        self._close_after_analysis = True
        self._analysis_queue.clear()
        self.database.clear_analysis_queue()
        for thread, _ in self._analysis_jobs.values():
            thread.requestInterruption()
        for thread, _ in self._style_jobs.values():
            thread.requestInterruption()
        for thread, _ in self._live_jobs.values():
            thread.requestInterruption()
        for thread, _ in self._regroup_jobs.values():
            thread.requestInterruption()
        if self._manual_link_job is not None:
            self._manual_link_job[0].requestInterruption()
        if self._active_jobs():
            self.status_label.setText("AI 작업 취소 후 프로그램을 종료합니다…")
            event.ignore()
        else:
            event.accept()

    def _active_jobs(self) -> bool:
        return bool(
            self._analysis_jobs
            or self._analysis_queue
            or self._style_jobs
            or self._live_jobs
            or self._regroup_jobs
            or self._manual_link_job is not None
        )
