from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from pathlib import Path

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
    live_capture_position,
    load_cached_transcript,
    record_live_reconnect_gap,
    remove_timeline_generation_checkpoint,
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
    AUTO_ANALYZE_SETTING,
    CACHE_RETENTION_SETTING,
    DISCOVERY_INTERVAL_SETTING,
    NEW_VOD_NOTIFICATION_SETTING,
    PRIVACY_NOTICE_SETTING,
    PRIVACY_NOTICE_VERSION,
    normalized_auto_analyze_mode,
    normalized_cache_retention,
    normalized_discovery_interval,
    setting_enabled,
)
from ..services.review_feedback import (
    REVIEW_FEEDBACK_ENABLED_SETTING,
    learn_review_feedback,
)
from ..services.transcription import (
    GPU_ADDON_DOWNLOAD_URL,
    detect_whisper_runtime,
    format_timestamp,
)
from ..services.timeline_validation import parse_duration_text
from ..services.timeline_document import (
    DEFAULT_TIMELINE_NOTICE,
    TIMELINE_NOTICE_SETTING,
    set_timeline_notice,
)
from ..services.update_checker import (
    UpdateInfo,
    automatic_update_check_enabled,
    configured_manifest_url,
    parse_update_manifest,
)
from ..services.update_installer import installer_download_path
from .analysis_worker import AnalysisWorker, PreTranscribeWorker
from .comment_publisher_window import SoopCommentPublisher
from .line_rewrite_worker import TimelineLineRewriteWorker
from .live_worker import LiveAnalysisWorker
from .manual_link_worker import ManualLinkWorker
from .regroup_worker import TimelineRegroupWorker
from .settings_dialog import AnalysisSettingsDialog
from .style_worker import TimelineStyleWorker
from .timeline_editor import TimelineDocumentEditor
from .transcript_viewer_dialog import TranscriptViewerDialog
from .update_worker import UpdateInstallerDownloadWorker
from .version_history_dialog import TimelineVersionHistoryDialog


logger = logging.getLogger(__name__)
GPU_ADDON_PROMPT_SETTING = "gpu_addon_prompt_version"


class MainWindow(QMainWindow):
    _REPLAY_LINK_RETRY_MS = (30_000, 90_000, 180_000, 300_000, 600_000, 1_200_000)
    _LIVE_RECONNECT_RETRY_MS = (5_000, 15_000, 30_000, 60_000, 180_000)
    _MAX_LIVE_RECONNECT_ATTEMPTS = len(_LIVE_RECONNECT_RETRY_MS)
    # faster-whisper is cached as one backend with one inference worker. Starting
    # several callers only queues on that backend while retaining extra decoders
    # and PCM buffers, so serialize background preparation.
    _MAX_CONCURRENT_PRETRANSCRIBES = 1
    _VOD_HEADER_SORT_KEYS = {
        1: ("state", "상태"),
        2: ("streamer", "스트리머"),
        3: ("title", "영상 제목"),
        4: ("memo", "메모"),
        5: ("duration", "길이"),
        6: ("published", "업로드"),
        7: ("vod_id", "영상/세션 번호"),
    }

    def __init__(self, database: Database, parent: QWidget | None = None):
        super().__init__(parent)
        self.database = database
        set_timeline_notice(
            database.get_setting(TIMELINE_NOTICE_SETTING, DEFAULT_TIMELINE_NOTICE)
        )
        self.analyzer: TimelineAnalyzer = LocalWhisperGeminiAnalyzer.from_database(database)
        self.styler = AITimelineStyler.from_database(database)
        self.discovery = SoopVodDiscovery(self)
        self._actual_new_count = 0
        self._vod_header_sort_column: int | None = None
        self._vod_header_sort_ascending = True
        self._editor_tabs: dict[str, TimelineDocumentEditor] = {}
        self._analysis_jobs: dict[str, tuple[QThread, AnalysisWorker]] = {}
        self._analysis_source_ids: dict[str, str] = {}
        self._analysis_previous_states: dict[str, tuple[str, str]] = {}
        self._analysis_background_fw_ready: set[str] = set()
        self._live_jobs: dict[str, tuple[QThread, LiveAnalysisWorker]] = {}
        self._live_shutdown_resume_ids: set[str] = set()
        self._live_reconnect_job: tuple[QThread, ManualLinkWorker] | None = None
        self._live_reconnect_target_id = ""
        self._live_reconnect_source: LiveAudioSource | None = None
        self._live_reconnect_error = ""
        self._live_reconnect_attempts: dict[str, int] = {}
        self._cancelled_live_reconnect_ids: set[str] = set()
        self._live_reconnect_retry_scheduled = False
        self._style_jobs: dict[str, tuple[QThread, TimelineStyleWorker]] = {}
        self._line_rewrite_jobs: dict[str, tuple[QThread, TimelineLineRewriteWorker]] = {}
        self._regroup_jobs: dict[str, tuple[QThread, TimelineRegroupWorker]] = {}
        self._manual_link_job: tuple[QThread, ManualLinkWorker] | None = None
        self._pending_reanalysis_live_id = ""
        self._pending_reanalysis_start: tuple[str, str] | None = None
        self._transcript_windows: dict[str, TranscriptViewerDialog] = {}
        self._comment_publishers: dict[str, SoopCommentPublisher] = {}
        self._comment_publish_acknowledged = False
        self._analysis_queue: list[str] = self.database.recover_analysis_queue()
        # Background auto faster-whisper (no Gemini) for newly discovered VODs.
        self._pretranscribe_queue: list[str] = []
        self._pretranscribe_jobs: dict[
            str,
            tuple[QThread, PreTranscribeWorker],
        ] = {}
        # A queued VOD gets at most one background FW attempt while another
        # analysis owns Gemini. Without this guard, a completed cache-only
        # worker is removed from the queue and immediately re-enqueued,
        # creating and destroying QThreads in a tight loop.
        self._pretranscribe_attempted_ids: set[str] = set()
        self._new_vod_ids_this_run: list[str] = []
        self._close_after_analysis = False
        self._loading_more_vods = False
        self._linked_replay_count = 0
        self._replay_link_attempts: dict[str, int] = {}
        self._replay_link_scheduled: set[str] = set()
        self._replay_link_inflight: set[str] = set()
        self._replay_merge_scheduled: set[str] = set()
        self._update_reply: QNetworkReply | None = None
        self._update_check_silent = True
        self._update_download_job: tuple[
            QThread,
            UpdateInstallerDownloadWorker,
        ] | None = None
        self._downloaded_update_path = ""
        self._pending_update_installer_path = ""
        self._quit_after_update_cancel = False
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

        self._force_quit = False
        self._tray_hint_shown = False
        self.tray_icon: QSystemTrayIcon | None = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            icon = self.windowIcon()
            if icon.isNull():
                icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
            self.tray_icon = QSystemTrayIcon(icon, self)
            self.tray_icon.setToolTip("SOOP AI 타임라인")
            tray_menu = QMenu(self)
            tray_menu.addAction("열기").triggered.connect(self._show_from_tray)
            tray_menu.addAction("종료").triggered.connect(self._quit_from_tray)
            self.tray_icon.setContextMenu(tray_menu)
            self.tray_icon.activated.connect(self._on_tray_activated)
            self.tray_icon.show()
            app = QApplication.instance()
            if app is not None:
                # The close button hides to the tray, so don't quit on window close.
                app.setQuitOnLastWindowClosed(False)

        for live_vod_id, replay_vod_id in (
            self.database.list_pending_live_replay_migrations()
        ):
            self._apply_linked_replay(live_vod_id, replay_vod_id)

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
        QTimer.singleShot(6_000, self._offer_gpu_addon_if_needed)
        if self._stale_live_sessions:
            self.status_label.setText(
                f"중단된 라이브 세션 {len(self._stale_live_sessions):,}개에 "
                "자동으로 다시 연결합니다…"
            )
            QTimer.singleShot(500, self._resume_stale_live_sessions)
        for live_vod_id in self.database.list_recent_unlinked_live_sessions():
            self._schedule_replay_link_check(live_vod_id, initial=True)

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
            "SOOP 약관을 확인한 영상에만 사용하세요. 이 앱은 SOOP 비공식 도구이며 "
            "SOOP과 제휴·승인 관계가 없습니다. 설정에서 캐시를 삭제할 수 있습니다."
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
            "검수 후 블록별로 복사하거나, 편집 탭의 'SOOP에 작성'으로 로그인해 "
            "댓글·대댓글을 등록할 수 있습니다. 자동 등록은 공식 API가 아닌 본인 로그인 세션을 사용합니다."
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
        self.streamer_list.currentItemChanged.connect(
            self._select_streamer_tab_from_list
        )
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
        self.filter_combo.addItem("숨긴 영상", "hidden")
        self.filter_combo.currentIndexChanged.connect(self.load_vods)
        self.sort_combo = QComboBox()
        self.sort_combo.addItem("최신 영상순", "newest")
        self.sort_combo.addItem("오래된 영상순", "oldest")
        self.sort_combo.addItem("최근 작업순", "recent_work")
        self.sort_combo.addItem("상태순", "status")
        self.sort_combo.currentIndexChanged.connect(
            self._on_vod_sort_combo_changed
        )
        self.load_more_button = QPushButton("과거 영상 30개 더 불러오기")
        self.load_more_button.clicked.connect(self.load_more_vods)
        clear_button = QPushButton("선택 해제")
        clear_button.clicked.connect(self.clear_checks)
        controls.addWidget(title)
        controls.addWidget(self.vod_count_label)
        controls.addStretch(1)
        controls.addWidget(self.sort_combo)
        controls.addWidget(self.filter_combo)
        controls.addWidget(self.load_more_button)
        controls.addWidget(clear_button)
        layout.addLayout(controls)

        self.streamer_tabs = QTabBar()
        self.streamer_tabs.setExpanding(False)
        self.streamer_tabs.setUsesScrollButtons(True)
        self.streamer_tabs.currentChanged.connect(self._on_streamer_tab_changed)
        layout.addWidget(self.streamer_tabs)

        self.vod_table = QTableWidget(0, 8)
        self.vod_table.setHorizontalHeaderLabels(
            [
                "선택",
                "상태",
                "스트리머",
                "영상 제목",
                "메모",
                "길이",
                "업로드",
                "영상/세션 번호",
            ]
        )
        self.vod_table.setToolTip(
            "열 제목을 클릭하면 오름차순·내림차순으로 정렬합니다. "
            "더블클릭하면 타임라인 작업 탭을 엽니다. "
            "오른쪽 클릭하면 SOOP 열기와 목록 숨기기를 사용할 수 있습니다."
        )
        self.vod_table.setAlternatingRowColors(True)
        self.vod_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.vod_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.vod_table.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.vod_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.vod_table.setSortingEnabled(False)
        self.vod_table.verticalHeader().setVisible(False)
        self.vod_table.verticalHeader().setDefaultSectionSize(43)
        self.vod_table.cellDoubleClicked.connect(self.open_vod_from_row)
        self.vod_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.vod_table.customContextMenuRequested.connect(self._show_vod_context_menu)

        header = self.vod_table.horizontalHeader()
        # ResizeToContents lets one unusually long value consume the viewport and
        # can squeeze the title header down to only a few pixels. Keep predictable
        # user-resizable widths instead; narrow windows use horizontal scrolling.
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setStretchLastSection(False)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(False)
        header.sectionClicked.connect(self._on_vod_header_clicked)
        for column, width in enumerate((48, 76, 96, 235, 130, 70, 145, 115)):
            self.vod_table.setColumnWidth(column, width)
        layout.addWidget(self.vod_table, 1)
        return panel

    def load_streamers(self) -> None:
        selected_streamer_id = self._current_streamer_id()
        self.streamer_list.clear()
        streamers = self.database.list_streamers()
        for streamer in (item for item in streamers if item.enabled):
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

        self.streamer_tabs.blockSignals(True)
        while self.streamer_tabs.count():
            self.streamer_tabs.removeTab(0)
        for streamer in streamers:
            index = self.streamer_tabs.addTab(streamer.display_name)
            self.streamer_tabs.setTabData(index, streamer.id)
            self.streamer_tabs.setTabToolTip(index, f"@{streamer.channel_id}")
        if self.streamer_tabs.count() == 0:
            index = self.streamer_tabs.addTab("등록된 스트리머 없음")
            self.streamer_tabs.setTabData(index, None)
            self.streamer_tabs.setTabEnabled(index, False)
        else:
            target = next(
                (
                    index
                    for index in range(self.streamer_tabs.count())
                    if self.streamer_tabs.tabData(index) == selected_streamer_id
                ),
                0,
            )
            self.streamer_tabs.setCurrentIndex(target)
        self.streamer_tabs.blockSignals(False)
        self._update_load_more_button()

    def _on_vod_sort_combo_changed(self, index: int) -> None:
        del index
        self._vod_header_sort_column = None
        if hasattr(self, "vod_table"):
            self.vod_table.horizontalHeader().setSortIndicatorShown(False)
        self.load_vods()

    def _on_vod_header_clicked(self, column: int) -> None:
        sort_spec = self._VOD_HEADER_SORT_KEYS.get(column)
        if sort_spec is None:
            return
        if self._vod_header_sort_column == column:
            self._vod_header_sort_ascending = (
                not self._vod_header_sort_ascending
            )
        else:
            self._vod_header_sort_column = column
            self._vod_header_sort_ascending = True

        order = (
            Qt.SortOrder.AscendingOrder
            if self._vod_header_sort_ascending
            else Qt.SortOrder.DescendingOrder
        )
        header = self.vod_table.horizontalHeader()
        header.setSortIndicatorShown(True)
        header.setSortIndicator(column, order)

        _, label = sort_spec
        direction = "오름차순" if self._vod_header_sort_ascending else "내림차순"
        self.sort_combo.blockSignals(True)
        self.sort_combo.setCurrentIndex(-1)
        self.sort_combo.setPlaceholderText(f"{label} {direction}")
        self.sort_combo.blockSignals(False)
        self.load_vods()

    def load_vods(self) -> None:
        checked_vod_ids = set(self.selected_vod_ids())
        mode = self.filter_combo.currentData() if hasattr(self, "filter_combo") else "work"
        hidden = mode == "hidden"
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

        streamer_id = self._current_streamer_id()
        sort = self.sort_combo.currentData() if hasattr(self, "sort_combo") else "newest"
        header_sort = self._VOD_HEADER_SORT_KEYS.get(
            self._vod_header_sort_column
        )
        if header_sort is not None:
            direction = "asc" if self._vod_header_sort_ascending else "desc"
            sort = f"{header_sort[0]}_{direction}"
        vods = self.database.list_vods(
            states=states,
            streamer_id=streamer_id,
            sort=str(sort),
            hidden=hidden,
        )
        self.vod_table.setUpdatesEnabled(False)
        self.vod_table.blockSignals(True)
        self.vod_table.setRowCount(len(vods))
        try:
            for row, vod in enumerate(vods):

                check_item = QTableWidgetItem()
                check_item.setFlags(
                    check_item.flags()
                    | Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsEnabled
                )
                check_item.setCheckState(
                    Qt.CheckState.Checked
                    if vod.vod_id in checked_vod_ids
                    else Qt.CheckState.Unchecked
                )
                check_item.setData(Qt.ItemDataRole.UserRole, vod.vod_id)
                check_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.vod_table.setItem(row, 0, check_item)

                memo = " ".join(vod.memo.split())
                memo_preview = memo if len(memo) <= 80 else f"{memo[:79]}…"
                values = [
                    STATE_LABELS.get(vod.state, vod.state),
                    vod.streamer_name,
                    vod.title,
                    memo_preview,
                    vod.duration_text,
                    vod.published_text,
                    vod.vod_id,
                ]
                for column, value in enumerate(values, start=1):
                    item = QTableWidgetItem(value)
                    item.setData(Qt.ItemDataRole.UserRole, vod.vod_id)
                    if column == 4 and memo:
                        item.setToolTip(vod.memo)
                    elif column in (3, 6) and value:
                        item.setToolTip(value)
                    if column in (1, 5, 6, 7):
                        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self.vod_table.setItem(row, column, item)
        finally:
            self.vod_table.blockSignals(False)
            self.vod_table.setUpdatesEnabled(True)
            self.vod_table.viewport().update()

        self.vod_count_label.setText(f"{len(vods):,}개")
        self._update_load_more_button()

    def _current_streamer_id(self) -> int | None:
        if not hasattr(self, "streamer_tabs") or self.streamer_tabs.count() == 0:
            return None
        value = self.streamer_tabs.tabData(self.streamer_tabs.currentIndex())
        return int(value) if value is not None else None

    def _select_streamer_tab(self, streamer_id: int) -> None:
        for index in range(self.streamer_tabs.count()):
            if self.streamer_tabs.tabData(index) == streamer_id:
                self.streamer_tabs.setCurrentIndex(index)
                return

    def _select_streamer_tab_from_list(
        self,
        current: QListWidgetItem | None,
        previous: QListWidgetItem | None,
    ) -> None:
        del previous
        if current is None:
            return
        self._select_streamer_tab(int(current.data(Qt.ItemDataRole.UserRole)))

    def _on_streamer_tab_changed(self, index: int) -> None:
        del index
        streamer_id = self._current_streamer_id()
        if streamer_id is not None:
            self.streamer_list.blockSignals(True)
            for row in range(self.streamer_list.count()):
                item = self.streamer_list.item(row)
                if int(item.data(Qt.ItemDataRole.UserRole)) == streamer_id:
                    self.streamer_list.setCurrentRow(row)
                    break
            self.streamer_list.blockSignals(False)
        self.load_vods()

    def _update_load_more_button(self) -> None:
        if not hasattr(self, "load_more_button"):
            return
        streamer_id = self._current_streamer_id()
        streamer = self.database.get_streamer(streamer_id) if streamer_id is not None else None
        hidden_mode = (
            hasattr(self, "filter_combo")
            and self.filter_combo.currentData() == "hidden"
        )
        enabled = bool(
            streamer
            and streamer.enabled
            and not self.discovery.busy
            and not hidden_mode
        )
        self.load_more_button.setEnabled(enabled)
        self.load_more_button.setToolTip(
            "현재 스트리머의 목록을 아래로 탐색해 과거 영상 30개를 추가합니다."
            if enabled
            else (
                "숨긴 영상 보기에서는 과거 영상을 불러올 수 없습니다."
                if hidden_mode
                else "자동 확인 목록에 등록된 스트리머 탭에서 사용할 수 있습니다."
            )
        )

    def load_more_vods(self) -> None:
        if self.discovery.busy:
            self.status_label.setText("현재 영상 확인이 끝난 뒤 과거 영상을 불러오세요.")
            return
        streamer_id = self._current_streamer_id()
        streamer = self.database.get_streamer(streamer_id) if streamer_id is not None else None
        if streamer is None or not streamer.enabled:
            self.status_label.setText(
                "자동 확인 스트리머 탭을 선택해야 과거 영상을 불러올 수 있습니다."
            )
            return
        self._loading_more_vods = True
        self._actual_new_count = 0
        self._linked_replay_count = 0
        known_ids = set(self.database.list_vod_ids_for_streamer(streamer.id))
        self.discovery.load_more(streamer, known_ids)

    def add_streamer(self) -> None:
        try:
            channel_id = normalize_channel_id(self.channel_input.text())
        except ValueError as error:
            QMessageBox.information(self, "입력 확인", str(error))
            return

        streamer = self.database.add_streamer(channel_id, self.name_input.text())
        self.channel_input.clear()
        self.name_input.clear()
        self.load_streamers()
        self._select_streamer_tab(streamer.id)
        self.status_label.setText(f"@{channel_id}을(를) 추가했습니다.")
        QTimer.singleShot(100, self.refresh_discovery)

    def remove_streamer(self) -> None:
        current = self.streamer_list.currentItem()
        if current is None:
            QMessageBox.information(self, "선택 필요", "삭제할 스트리머를 선택하세요.")
            return
        if self.discovery.busy:
            QMessageBox.information(
                self,
                "신규 영상 확인 중",
                "신규 영상 확인이 끝난 뒤 스트리머를 삭제하세요.",
            )
            return
        streamer_id = int(current.data(Qt.ItemDataRole.UserRole))
        vod_ids = self.database.list_vod_ids_for_streamer(streamer_id)
        active_vod_ids = (
            set(self._analysis_jobs)
            | set(self._analysis_queue)
            | set(self._live_jobs)
            | set(self._style_jobs)
            | set(self._line_rewrite_jobs)
            | set(self._regroup_jobs)
            | set(self._pretranscribe_jobs)
            | set(self._pretranscribe_queue)
            | set(self._stale_live_sessions)
        )
        if self._live_reconnect_target_id:
            active_vod_ids.add(self._live_reconnect_target_id)
        active_streamer_vods = active_vod_ids.intersection(vod_ids)
        if active_streamer_vods:
            QMessageBox.information(
                self,
                "AI 작업 진행 중",
                self._ai_busy_message(
                    "이 작업이 끝난 뒤 스트리머를 삭제하세요.",
                    preferred_vod_id=next(iter(active_streamer_vods)),
                ),
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
            "예: 홍길동\n월드 오브 워크래프트\n약칭 = 정식 표기",
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
        if getattr(self, "_close_after_analysis", False):
            return
        if self._manual_link_job is not None:
            self.status_label.setText("이미 수동 링크를 확인하고 있습니다.")
            return
        if self._live_reconnect_job is not None or self._stale_live_sessions:
            self.status_label.setText(
                "기존 라이브 자동 재연결을 먼저 처리하고 있습니다."
            )
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
                or self._line_rewrite_jobs
                or self._live_jobs
                or self._regroup_jobs
                or self._pretranscribe_jobs
                or self._pretranscribe_queue
            ):
                QMessageBox.information(
                    self,
                    "AI 작업 진행 중",
                    self._ai_busy_message(
                        "이 작업이 끝난 뒤 라이브 실시간 분석을 시작하세요."
                    ),
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
        if getattr(self, "_close_after_analysis", False):
            self._pending_reanalysis_live_id = ""
            self._pending_reanalysis_start = None
            return
        if isinstance(result, ResolvedVodLink):
            was_new = self.database.get_vod(result.vod_id) is None
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
                live_broadcast_no=result.source_broadcast_no,
            )
            reanalysis_live_id = self._pending_reanalysis_live_id
            if reanalysis_live_id:
                self._pending_reanalysis_live_id = ""
                try:
                    self.database.link_live_session_to_replay(
                        reanalysis_live_id,
                        vod.vod_id,
                    )
                except ValueError as error:
                    self._manual_link_failed(str(error))
                    return
                self._apply_linked_replay(reanalysis_live_id, vod.vod_id)
                self._pending_reanalysis_start = (
                    reanalysis_live_id,
                    vod.vod_id,
                )
                self.manual_link_input.clear()
                self.load_streamers()
                self._select_streamer_tab(vod.streamer_id)
                self.load_vods()
                self.open_timeline(vod.vod_id)
                self.status_label.setText(
                    "다시보기를 연결하고 라이브 작업을 옮겼습니다. 전체 분석을 준비합니다."
                )
                return

            links = self.database.auto_link_live_sessions(
                vod.streamer_id,
                [vod.vod_id],
                new_vod_ids=[vod.vod_id] if was_new else [],
            )
            for live_vod_id, replay_vod_id in links:
                self._apply_linked_replay(live_vod_id, replay_vod_id)
            self.manual_link_input.clear()
            self.load_streamers()
            self._select_streamer_tab(vod.streamer_id)
            self.load_vods()
            self.open_timeline(vod.vod_id)
            self.status_label.setText(
                "수동 다시보기를 추가했습니다. 고속 AI 분석을 시작합니다."
            )
            QTimer.singleShot(0, lambda: self.start_analysis(vod.vod_id))
            return

        if isinstance(result, LiveAudioSource):
            if (
                self._analysis_jobs
                or self._analysis_queue
                or self._style_jobs
                or self._line_rewrite_jobs
                or self._live_jobs
                or self._regroup_jobs
                or self._pretranscribe_jobs
                or self._pretranscribe_queue
            ):
                QMessageBox.information(
                    self,
                    "AI 작업 진행 중",
                    self._ai_busy_message(
                        "작업 상태가 바뀌어 라이브 항목을 만들지 않았습니다. "
                        "현재 작업이 끝난 뒤 다시 연결하세요."
                    ),
                )
                return
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
                live_broadcast_no=result.broadcast_no,
            )
            self.manual_link_input.clear()
            self.load_streamers()
            self._select_streamer_tab(vod.streamer_id)
            self.load_vods()
            self.open_timeline(vod.vod_id)
            self.start_live_analysis(vod.vod_id, result)
            return

        self._manual_link_failed("지원하지 않는 링크 확인 결과입니다.")

    @Slot(str)
    def _manual_link_failed(self, message: str) -> None:
        self._pending_reanalysis_live_id = ""
        self._pending_reanalysis_start = None
        if getattr(self, "_close_after_analysis", False):
            return
        self.status_label.setText(f"수동 링크 확인 실패: {message}")
        QMessageBox.critical(self, "수동 링크 확인 실패", message)

    @Slot()
    def _manual_link_thread_finished(self) -> None:
        self._manual_link_job = None
        self.manual_link_button.setEnabled(True)
        self.manual_link_input.setEnabled(True)
        pending_reanalysis = self._pending_reanalysis_start
        self._pending_reanalysis_start = None
        if pending_reanalysis is not None:
            if self._close_after_analysis:
                if not self._active_jobs():
                    QTimer.singleShot(0, self.close)
                return
            live_vod_id, replay_vod_id = pending_reanalysis
            QTimer.singleShot(
                0,
                lambda live_id=live_vod_id, replay_id=replay_vod_id:
                self._start_linked_replay_reanalysis(live_id, replay_id),
            )
            return
        if self._close_after_analysis and not self._active_jobs():
            QTimer.singleShot(0, self.close)
        else:
            self._resume_analysis_queue_if_idle()

    def refresh_discovery(self) -> None:
        if self.discovery.busy:
            self.status_label.setText("이미 신규 영상을 확인하고 있습니다.")
            return
        streamers = self.database.list_streamers(enabled_only=True)
        if not streamers:
            self.status_label.setText("먼저 자동 확인할 스트리머를 추가하세요.")
            return
        self._loading_more_vods = False
        self._actual_new_count = 0
        self._linked_replay_count = 0
        self._new_vod_ids_this_run = []
        self.discovery.refresh(streamers)

    def _initial_refresh(self) -> None:
        if normalized_discovery_interval(
            self.database.get_setting(DISCOVERY_INTERVAL_SETTING, "180")
        ) > 0 and self.database.list_streamers(enabled_only=True):
            self.refresh_discovery()

    def _on_discovery_started(self, count: int) -> None:
        self.refresh_button.setEnabled(False)
        self._update_load_more_button()
        self.status_label.setText(
            "선택한 스트리머의 과거 영상을 불러옵니다…"
            if self._loading_more_vods
            else f"스트리머 {count}명의 신규 영상을 확인합니다…"
        )

    def _on_discovery_result(self, streamer_id: int, streamer_name: str, items: object) -> None:
        if streamer_name:
            self.database.update_streamer_name(streamer_id, streamer_name)
        if isinstance(items, list):
            item_ids = [
                str(item.get("vod_id", "") or "")
                for item in items
                if isinstance(item, dict)
            ]
            new_ids = [
                vod_id
                for vod_id in item_ids
                if vod_id and self.database.get_vod(vod_id) is None
            ]
            if not self._loading_more_vods:
                self._new_vod_ids_this_run.extend(new_ids)
            self._actual_new_count += self.database.upsert_discovered_vods(streamer_id, items)
            links = self.database.auto_link_live_sessions(
                streamer_id,
                item_ids,
                new_vod_ids=new_ids,
            )
            self._linked_replay_count += len(links)
            for live_vod_id, replay_vod_id in links:
                self._apply_linked_replay(live_vod_id, replay_vod_id)
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
        if self._loading_more_vods:
            if error_count:
                self.status_label.setText("과거 영상을 불러오지 못했습니다.")
            elif self._actual_new_count:
                self.status_label.setText(
                    f"과거 영상 {self._actual_new_count}개를 추가했습니다."
                )
            else:
                self.status_label.setText("더 불러올 과거 영상이 없습니다.")
        elif error_count:
            self.status_label.setText(
                f"신규 {self._actual_new_count}개 · 확인 오류 {error_count}명"
            )
        else:
            linked = (
                f" · 종료된 라이브 {self._linked_replay_count}개 작업 통합"
                if self._linked_replay_count
                else ""
            )
            self.status_label.setText(
                f"신규 영상 {self._actual_new_count}개를 추가했습니다{linked}."
            )
        self._loading_more_vods = False
        self._update_load_more_button()
        self._finish_replay_link_checks()
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
        self._enqueue_auto_processing()

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
            self._sync_editor_analysis_state(vod_id)
            self.tabs.setCurrentWidget(existing_editor)
            return

        vod = self.database.get_vod(vod_id)
        if vod is None:
            return
        text = self._load_or_create_timeline_text(vod)
        editor = self._create_timeline_editor(vod, text)
        self._editor_tabs[vod_id] = editor
        self._refresh_editor_cache_state(vod_id)
        self._sync_editor_analysis_state(vod_id)
        title = vod.title if len(vod.title) <= 22 else f"{vod.title[:21]}…"
        index = self.tabs.addTab(editor, title)
        self.tabs.setTabToolTip(index, vod.title)
        self.tabs.setCurrentIndex(index)

    def _create_timeline_editor(
        self,
        vod: Vod,
        text: str,
    ) -> TimelineDocumentEditor:
        editor = TimelineDocumentEditor(
            vod,
            text,
            self.analyzer.available,
            self.analyzer.unavailable_reason,
            self.styler.available,
            self.styler.unavailable_reason,
        )
        if vod.linked_vod_id:
            replay = self.database.get_vod(vod.linked_vod_id)
            if replay is not None:
                editor.attach_replay(replay)
        editor.document_changed.connect(self._save_timeline)
        editor.memo_changed.connect(self._save_vod_memo)
        editor.review_completed.connect(self._mark_review_complete)
        editor.analysis_requested.connect(self.start_analysis)
        editor.analysis_cancel_requested.connect(self._cancel_or_dequeue)
        editor.live_reconnect_requested.connect(self.reconnect_live_session)
        editor.reanalyze_as_vod_requested.connect(self.reanalyze_live_as_vod)
        editor.style_requested.connect(self.start_style_correction)
        editor.line_rewrite_requested.connect(self.start_line_rewrite)
        editor.regroup_requested.connect(self.start_topic_regroup)
        editor.snapshot_requested.connect(self._snapshot_timeline)
        editor.version_history_requested.connect(self.show_version_history)
        editor.transcript_requested.connect(self.show_cached_transcript)
        editor.cache_delete_requested.connect(self.delete_vod_cache)
        editor.work_reset_requested.connect(self.reset_vod_work)
        editor.publish_requested.connect(self.open_comment_publisher)
        return editor

    def _replace_live_tab_with_replay(
        self,
        live_vod_id: str,
        replay_vod_id: str,
        document: str,
        *,
        status_message: str = (
            "전체 다시보기 분석 완료 · 라이브 분석본은 버전 기록에 보존했습니다."
        ),
    ) -> TimelineDocumentEditor | None:
        live_editor = self._editor_tabs.get(live_vod_id)
        replay = self.database.get_vod(replay_vod_id)
        if live_editor is None or replay is None:
            return None
        self._editor_tabs.pop(live_vod_id, None)

        live_index = self.tabs.indexOf(live_editor)
        live_memo = live_editor.memo_text().strip()
        live_editor.flush_memo_save()

        for target_id in (live_vod_id, replay_vod_id):
            publisher = self._comment_publishers.pop(target_id, None)
            if publisher is not None:
                publisher.close()
            transcript_window = self._transcript_windows.pop(target_id, None)
            if transcript_window is not None:
                transcript_window.close()

        duplicate = self._editor_tabs.pop(replay_vod_id, None)
        if duplicate is not None and duplicate is not live_editor:
            duplicate_index = self.tabs.indexOf(duplicate)
            duplicate.flush_memo_save()
            duplicate.close_review_player()
            duplicate.blockSignals(True)
            if duplicate_index >= 0:
                self.tabs.removeTab(duplicate_index)
                if 0 <= duplicate_index < live_index:
                    live_index -= 1
            duplicate.deleteLater()

        if live_memo and not replay.memo.strip():
            self.database.update_vod_memo(replay_vod_id, live_memo)
            replay = self.database.get_vod(replay_vod_id) or replay

        live_editor.close_review_player()
        live_editor.blockSignals(True)
        if live_index >= 0:
            self.tabs.removeTab(live_index)
        live_editor.deleteLater()

        replay_editor = self._create_timeline_editor(replay, document)
        self._editor_tabs[replay_vod_id] = replay_editor
        title = (
            replay.title
            if len(replay.title) <= 22
            else f"{replay.title[:21]}…"
        )
        if live_index >= 0:
            new_index = self.tabs.insertTab(live_index, replay_editor, title)
        else:
            new_index = self.tabs.addTab(replay_editor, title)
        self.tabs.setTabToolTip(new_index, replay.title)
        self.tabs.setCurrentIndex(new_index)
        self._refresh_editor_cache_state(replay_vod_id)
        replay_editor.status_label.setText(status_message)
        return replay_editor

    def open_comment_publisher(self, vod_id: str) -> None:
        vod = self.database.get_vod(vod_id)
        editor = self._editor_tabs.get(vod_id)
        if vod is None or editor is None:
            return
        publish_vod = vod
        if vod.source_kind == "live":
            publish_vod = self._linked_replay_for(vod)
            if publish_vod is None:
                QMessageBox.information(
                    self,
                    "다시보기 연결 필요",
                    "라이브 분석 기록은 완성된 다시보기를 연결한 뒤 댓글을 등록할 수 있습니다.",
                )
                return
        if not publish_vod.vod_id.isdigit():
            QMessageBox.critical(
                self,
                "댓글 등록 대상 오류",
                "댓글을 등록할 실제 SOOP 다시보기 번호를 확인하지 못했습니다.",
            )
            return
        blocks = [block for block in editor.blocks() if block.strip()]
        if not blocks:
            QMessageBox.information(
                self,
                "등록할 내용 없음",
                "먼저 타임라인을 분석·검수한 뒤 등록할 수 있습니다.",
            )
            return
        publisher_key = publish_vod.vod_id
        prior = self._comment_publishers.get(publisher_key)
        if prior is not None:
            if prior.is_busy:
                prior.show()
                prior.raise_()
                prior.activateWindow()
                self.status_label.setText(
                    "이 영상의 댓글을 이미 등록하고 있습니다. 기존 등록창을 표시했습니다."
                )
                return
            prior.cancel_publish(update_status=False)
            prior.close()
            if self._comment_publishers.get(publisher_key) is prior:
                self._comment_publishers.pop(publisher_key, None)
        if not self._comment_publish_acknowledged:
            proceed = QMessageBox.warning(
                self,
                "SOOP 로그인 등록 안내",
                "이 기능은 SOOP 공식 댓글 API가 아니라, 브라우저에 로그인한 회원님의 "
                "세션으로 댓글을 직접 등록합니다.\n\n"
                "· 비밀번호는 SOOP 로그인 페이지에서만 입력하며 앱은 저장하지 않습니다.\n"
                "· 자동 등록은 SOOP 이용약관에 저촉될 수 있으니 본인 책임으로 사용하세요.\n"
                "· 등록된 댓글은 SOOP에서 직접 삭제해야 합니다.\n\n"
                "계속하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if proceed != QMessageBox.StandardButton.Yes:
                return
            self._comment_publish_acknowledged = True

        reply_count = max(0, len(blocks) - 1)
        confirm = QMessageBox.question(
            self,
            "SOOP에 등록",
            f"'{publish_vod.title}' 영상에 댓글 1개"
            + (f"와 대댓글 {reply_count}개" if reply_count else "")
            + "를 지금 등록할까요?\n\n"
            "백그라운드에서 자동으로 등록하고, 끝나면 알림으로 알려드립니다. "
            "로그인이 풀려 있으면 로그인 창을 띄워 드립니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        window = SoopCommentPublisher(publish_vod, blocks)
        window.status_changed.connect(self.status_label.setText)
        window.published.connect(
            lambda message, target=publish_vod.vod_id: self._on_comment_published(
                target,
                message,
            )
        )
        window.closed.connect(
            lambda key=publisher_key, current=window: self._forget_comment_publisher(
                key,
                current,
            )
        )
        self._comment_publishers[publisher_key] = window
        window.open_in_background()

    def _forget_comment_publisher(
        self,
        key: str,
        publisher: SoopCommentPublisher,
    ) -> None:
        if self._comment_publishers.get(key) is publisher:
            self._comment_publishers.pop(key, None)

    def _on_comment_published(self, vod_id: str, message: str) -> None:
        self.database.set_vod_state(vod_id, VodState.PUBLISHED.value)
        self.load_vods()
        self.status_label.setText(f"SOOP 등록 완료 · {message}")
        if self.tray_icon is not None:
            self.tray_icon.showMessage(
                "SOOP 댓글 등록 완료",
                f"{message}\nSOOP 페이지에서 실제 반영을 확인해 주세요.",
                QSystemTrayIcon.MessageIcon.Information,
                6_000,
            )

    def _load_or_create_timeline_text(self, vod: Vod) -> str:
        document = self.database.get_timeline(vod.vod_id)
        if document is not None:
            return document.text
        text = self.analyzer.initial_document(vod)
        self.database.save_timeline(vod.vod_id, text, VodState.REVIEW.value)
        self.database.set_vod_state(vod.vod_id, VodState.REVIEW.value)
        return text

    def _linked_replay_for(self, vod: Vod) -> Vod | None:
        if vod.source_kind != "live" or not vod.linked_vod_id:
            return None
        return self.database.get_vod(vod.linked_vod_id)

    def _cache_source_vod(self, vod: Vod) -> Vod:
        replay = self._linked_replay_for(vod)
        if replay is not None and has_vod_cache(replay.vod_id):
            return replay
        return vod

    def _related_cache_vod_ids(self, vod: Vod) -> set[str]:
        """Return replay and retained live cache ids represented by one item."""

        related = {vod.vod_id}
        replay = self._linked_replay_for(vod)
        if replay is not None:
            related.add(replay.vod_id)
        broadcast_no = str(vod.live_broadcast_no or "").strip()
        source = replay or vod
        if not broadcast_no:
            broadcast_no = str(source.live_broadcast_no or "").strip()
        if broadcast_no:
            related.update(
                item.vod_id
                for item in self.database.list_live_sessions_for_broadcast(
                    source.streamer_id,
                    broadcast_no,
                )
            )
        return related

    def _refresh_editor_cache_state(self, vod_id: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        vod = self.database.get_vod(vod_id)
        if editor is None or vod is None:
            return
        cache_vod = self._cache_source_vod(vod)
        transcript_available = load_cached_transcript(cache_vod) is not None
        editor.set_cached_transcript_available(
            transcript_available,
            has_vod_cache(cache_vod.vod_id),
        )
        editor.set_final_pending(
            has_pending_timeline_finalization(cache_vod.vod_id)
        )

    def _refresh_all_editor_cache_states(self) -> None:
        for vod_id in list(self._editor_tabs):
            self._refresh_editor_cache_state(vod_id)

    def _active_analysis_target_id(self, vod_id: str) -> str | None:
        if vod_id in self._analysis_jobs:
            return vod_id
        for target_vod_id, source_vod_id in self._analysis_source_ids.items():
            if (
                source_vod_id == vod_id
                and target_vod_id in self._analysis_jobs
            ):
                return target_vod_id
        return None

    def _active_auxiliary_ai_job(
        self,
        vod_id: str,
    ) -> tuple[str, str, QThread] | None:
        job_groups = (
            ("문체 교정", self._style_jobs),
            ("한 줄 AI 변환", self._line_rewrite_jobs),
            ("주제 다시 묶기", self._regroup_jobs),
        )
        for job_name, jobs in job_groups:
            job = jobs.get(vod_id)
            if job is not None:
                return vod_id, job_name, job[0]

        for target_vod_id, job in self._regroup_jobs.items():
            target_vod = self.database.get_vod(target_vod_id)
            if target_vod is not None and target_vod.linked_vod_id == vod_id:
                return target_vod_id, "저장 자막 재정리", job[0]
        return None

    def _sync_editor_analysis_state(self, vod_id: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is None:
            return
        if vod_id in self._live_jobs:
            if not editor._live_running:
                editor.set_live_running(True)
            return

        target_vod_id = self._active_analysis_target_id(vod_id)
        if target_vod_id is not None:
            if not editor._analysis_running:
                editor.set_analysis_running(True)
                editor.set_analysis_progress(
                    0,
                    "AI 분석이 진행 중입니다. ‘분석 취소’로 중단할 수 있습니다.",
                )
            return

        if vod_id in self._analysis_queue:
            if vod_id in self._pretranscribe_jobs:
                message = "FW 자막추출 중 · 완료 후 Gemini 분석을 시작합니다."
            else:
                queue_position = self._analysis_queue.index(vod_id) + 1
                message = (
                    f"AI 분석 대기 중 · 대기열 {queue_position}번째 · "
                    "앞선 작업이 끝나면 자동으로 시작합니다."
                )
            editor.set_analysis_queued(message)
            return

        auxiliary_job = self._active_auxiliary_ai_job(vod_id)
        if auxiliary_job is not None:
            _, job_name, _ = auxiliary_job
            if not editor._auxiliary_ai_running:
                editor.set_auxiliary_ai_running(True, f"{job_name} 취소")
            return

        if editor._analysis_running:
            editor.set_analysis_running(False)
        if editor._auxiliary_ai_running:
            editor.set_auxiliary_ai_running(False)

    @Slot(str)
    def show_cached_transcript(self, vod_id: str) -> None:
        vod = self.database.get_vod(vod_id)
        if vod is None:
            return
        cache_vod = self._cache_source_vod(vod)
        transcript = load_cached_transcript(cache_vod)
        if transcript is None:
            QMessageBox.information(
                self,
                "저장 자막 없음",
                "완료된 Whisper 자막이 없습니다. 분석을 시작하거나 완료한 뒤 다시 확인하세요.",
            )
            self._refresh_editor_cache_state(vod_id)
            return
        existing = self._transcript_windows.get(vod_id)
        if existing is not None:
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return
        window = TranscriptViewerDialog(cache_vod, transcript)
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        window.finished.connect(
            lambda _result, target=vod_id: self._transcript_windows.pop(target, None)
        )
        self._transcript_windows[vod_id] = window
        window.show()
        window.raise_()
        window.activateWindow()

    def _apply_linked_replay(self, live_vod_id: str, replay_vod_id: str) -> None:
        replay = self.database.get_vod(replay_vod_id)
        if replay is None:
            return
        if self._vod_active_job(live_vod_id) or self._vod_active_job(replay_vod_id):
            if live_vod_id not in self._replay_merge_scheduled:
                self._replay_merge_scheduled.add(live_vod_id)
                QTimer.singleShot(
                    1_000,
                    lambda live_id=live_vod_id, replay_id=replay_vod_id: (
                        self._retry_linked_replay_merge(live_id, replay_id)
                    ),
                )
            return

        self._replay_merge_scheduled.discard(live_vod_id)
        live_editor = self._editor_tabs.get(live_vod_id)
        replay_editor = self._editor_tabs.get(replay_vod_id)
        if live_editor is not None:
            self._save_timeline(live_vod_id, live_editor.text())
            live_editor.flush_memo_save()
        if replay_editor is not None:
            self._save_timeline(replay_vod_id, replay_editor.text())
            replay_editor.flush_memo_save()

        try:
            self.database.migrate_live_session_work(
                live_vod_id,
                replay_vod_id,
            )
        except ValueError as error:
            logger.warning(
                "Could not migrate linked live work %s -> %s: %s",
                live_vod_id,
                replay_vod_id,
                error,
            )
            self.status_label.setText(f"라이브 작업 이전 실패: {error}")
            return

        document = self.database.get_timeline(replay_vod_id)
        migrated_text = document.text if document is not None else ""
        message = "라이브 작업을 새 다시보기 항목으로 이전했습니다."
        if live_editor is not None:
            self._replace_live_tab_with_replay(
                live_vod_id,
                replay_vod_id,
                migrated_text,
                status_message=message,
            )
        else:
            replay = self.database.get_vod(replay_vod_id) or replay
            if replay_editor is not None:
                replay_editor.set_text(migrated_text)
                replay_editor.memo_editor.blockSignals(True)
                replay_editor.memo_editor.setPlainText(replay.memo)
                replay_editor.memo_editor.blockSignals(False)
                replay_editor._last_saved_memo = replay.memo
                replay_editor.status_label.setText(message)

        self._replay_link_attempts.pop(live_vod_id, None)
        self._replay_link_inflight.discard(live_vod_id)

    def _retry_linked_replay_merge(
        self,
        live_vod_id: str,
        replay_vod_id: str,
    ) -> None:
        self._replay_merge_scheduled.discard(live_vod_id)
        self._apply_linked_replay(live_vod_id, replay_vod_id)

    def _schedule_replay_link_check(
        self,
        live_vod_id: str,
        *,
        initial: bool = False,
    ) -> None:
        vod = self.database.get_vod(live_vod_id)
        if vod is None or vod.source_kind != "live" or vod.linked_vod_id:
            if vod is not None and vod.linked_vod_id:
                self._apply_linked_replay(live_vod_id, vod.linked_vod_id)
            return
        if live_vod_id in self._replay_link_scheduled:
            return
        attempt = self._replay_link_attempts.get(live_vod_id, 0)
        if attempt >= len(self._REPLAY_LINK_RETRY_MS):
            return
        delay = 5_000 if initial else self._REPLAY_LINK_RETRY_MS[attempt]
        self._replay_link_scheduled.add(live_vod_id)
        QTimer.singleShot(
            delay,
            lambda target=live_vod_id: self._check_replay_link(target),
        )

    def _check_replay_link(self, live_vod_id: str) -> None:
        self._replay_link_scheduled.discard(live_vod_id)
        vod = self.database.get_vod(live_vod_id)
        if vod is None or vod.source_kind != "live":
            return
        if vod.linked_vod_id:
            self._apply_linked_replay(live_vod_id, vod.linked_vod_id)
            return
        if self.discovery.busy:
            self._schedule_replay_link_check(live_vod_id)
            return
        streamer = self.database.get_streamer(vod.streamer_id)
        if streamer is None:
            return
        self._replay_link_attempts[live_vod_id] = (
            self._replay_link_attempts.get(live_vod_id, 0) + 1
        )
        self._replay_link_inflight.add(live_vod_id)
        self._loading_more_vods = False
        self._actual_new_count = 0
        self._linked_replay_count = 0
        self.discovery.refresh([streamer], include_disabled=True)

    def _finish_replay_link_checks(self) -> None:
        pending = list(self._replay_link_inflight)
        self._replay_link_inflight.clear()
        for live_vod_id in pending:
            vod = self.database.get_vod(live_vod_id)
            if vod is None:
                continue
            if vod.linked_vod_id:
                self._apply_linked_replay(live_vod_id, vod.linked_vod_id)
            else:
                self._schedule_replay_link_check(live_vod_id)

    @Slot(str)
    def delete_vod_cache(self, vod_id: str) -> None:
        vod = self.database.get_vod(vod_id)
        if vod is None:
            return
        cache_ids = self._related_cache_vod_ids(vod)
        active_id = next(
            (candidate for candidate in cache_ids if self._vod_active_job(candidate)),
            None,
        )
        if active_id is not None:
            QMessageBox.information(
                self,
                "AI 작업 진행 중",
                self._ai_busy_message(
                    "이 작업이 끝난 뒤 캐시를 삭제하세요.",
                    preferred_vod_id=active_id,
                ),
            )
            return
        answer = QMessageBox.question(
            self,
            "이 영상 자막 캐시 삭제",
            "Whisper 자막과 AI 중간 결과를 삭제할까요?\n"
            "연결되어 숨겨진 라이브 자막 캐시도 함께 삭제합니다.\n"
            "현재 타임라인 문서는 유지됩니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed_count = sum(1 for cache_id in cache_ids if remove_vod_cache(cache_id))
        self._refresh_editor_cache_state(vod_id)
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.status_label.setText(
                f"관련 자막 캐시 {removed_count:,}개를 삭제했습니다."
                if removed_count
                else "삭제할 자막 캐시가 없습니다."
            )

    @Slot(str)
    def reset_vod_work(self, vod_id: str) -> None:
        if self._vod_active_job(vod_id):
            QMessageBox.information(
                self,
                "AI 작업 진행 중",
                self._ai_busy_message(
                    "이 작업을 취소하거나 완료한 뒤 기록을 초기화하세요.",
                    preferred_vod_id=vod_id,
                ),
            )
            return
        vod = self.database.get_vod(vod_id)
        if vod is None:
            return
        answer = QMessageBox.question(
            self,
            "작업 기록 초기화",
            f"'{vod.title}'의 현재 타임라인과 이전 버전 기록을 초기화할까요?\n"
            "영상은 신규 영상 목록에 그대로 남고 Whisper 자막 캐시도 유지됩니다.\n"
            "자막까지 지우려면 별도의 ‘자막 캐시 삭제’를 사용하세요.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if vod_id in self._analysis_queue:
            self._analysis_queue.remove(vod_id)
        self.database.reset_vod_work(vod_id)
        remove_timeline_generation_checkpoint(
            self._cache_source_vod(vod).vod_id
        )
        editor = self._editor_tabs.get(vod_id)
        initial = self.analyzer.initial_document(vod)
        if editor is not None:
            editor.reset_work_document(initial)
        self._refresh_editor_cache_state(vod_id)
        self.load_vods()
        self.status_label.setText(
            "작업 기록을 초기화했습니다. 영상 목록과 자막 캐시는 유지됩니다."
        )

    def _save_timeline(self, vod_id: str, text: str) -> None:
        if vod_id in self._live_jobs:
            self.database.save_timeline(
                vod_id,
                text,
                VodState.ANALYZING.value,
            )
            return
        vod = self.database.get_vod(vod_id)
        existing = self.database.get_timeline(vod_id)
        changed = existing is None or existing.text != text
        preserve_states = {
            VodState.READY.value,
            VodState.COPIED.value,
            VodState.PUBLISHED.value,
            VodState.SKIPPED.value,
        }
        if vod is not None and not changed and vod.state in preserve_states:
            self.database.save_timeline(vod_id, text, vod.state)
            return
        self.database.save_timeline(vod_id, text, VodState.REVIEW.value)
        if vod and vod.state != VodState.REVIEW.value:
            self.database.set_vod_state(vod_id, VodState.REVIEW.value)

    @Slot(str, str)
    def _save_vod_memo(self, vod_id: str, memo: str) -> None:
        self.database.update_vod_memo(vod_id, memo)
        preview = " ".join(memo.split())
        if len(preview) > 80:
            preview = f"{preview[:79]}…"
        for row in range(self.vod_table.rowCount()):
            id_item = self.vod_table.item(row, 0)
            if (
                id_item is None
                or str(id_item.data(Qt.ItemDataRole.UserRole)) != vod_id
            ):
                continue
            memo_item = self.vod_table.item(row, 4)
            if memo_item is not None:
                memo_item.setText(preview)
                memo_item.setToolTip(memo)
            break

    @Slot(str, str, str)
    def _snapshot_timeline(self, vod_id: str, reason: str, text: str) -> None:
        self.database.create_timeline_revision(vod_id, text, reason)

    @Slot(str)
    def show_version_history(self, vod_id: str) -> None:
        if self._vod_active_job(vod_id):
            QMessageBox.information(
                self,
                "AI 작업 진행 중",
                "현재 작업이 끝난 뒤 이전 버전을 복원하세요.",
            )
            return
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
        self.database.set_vod_state(vod_id, VodState.REVIEW.value)
        self.load_vods()
        editor.status_label.setText("선택한 버전으로 복원했습니다.")

    def _mark_review_complete(self, vod_id: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        reviewed_text = editor.text() if editor is not None else ""
        if editor is not None:
            self.database.save_timeline(vod_id, reviewed_text, VodState.READY.value)
        self.database.set_vod_state(vod_id, VodState.READY.value)
        feedback_message = "검수 피드백 학습이 꺼져 있습니다."
        if self.database.get_setting(REVIEW_FEEDBACK_ENABLED_SETTING, "1") != "0":
            vod = self.database.get_vod(vod_id)
            if vod is not None and reviewed_text.strip():
                try:
                    feedback_message = learn_review_feedback(
                        self.database,
                        vod,
                        reviewed_text,
                    ).summary()
                except Exception:
                    logger.exception("Review feedback learning failed for %s", vod_id)
                    feedback_message = "검수는 저장했지만 피드백 사례 저장에 실패했습니다."
        message = f"검수 완료 · {feedback_message}"
        self.status_label.setText(message)
        if editor is not None:
            QTimer.singleShot(
                0,
                lambda target=editor, text=message: target.status_label.setText(text),
            )
        self.load_vods()

    def _save_review_feedback_draft(self, vod_id: str, document: str) -> None:
        if self.database.get_setting(REVIEW_FEEDBACK_ENABLED_SETTING, "1") == "0":
            return
        self.database.save_review_feedback_draft(vod_id, document)

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
            self._resume_analysis_queue_if_idle()
        else:
            self.status_label.setText(self.analyzer.unavailable_reason)

    def check_for_updates(self, *, silent: bool = False) -> None:
        pending_installer = Path(self._pending_update_installer_path)
        if not silent and pending_installer.is_file():
            self._launch_downloaded_update(pending_installer)
            return
        if self._pending_update_installer_path and not pending_installer.is_file():
            self._pending_update_installer_path = ""
            self.update_button.setText("업데이트 확인")
        if self._update_download_job is not None:
            if not silent:
                self.status_label.setText("업데이트 설치 파일을 다운로드하고 있습니다…")
            return
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
        install_button = None
        if info.automatic_install_available:
            install_button = message.addButton(
                "다운로드 후 자동 업데이트",
                QMessageBox.ButtonRole.AcceptRole,
            )
        if info.download_url:
            open_button = message.addButton(
                "다운로드 페이지 열기",
                QMessageBox.ButtonRole.ActionRole,
            )
        else:
            open_button = None
            message.setInformativeText(
                (message.informativeText() + "\n\n" if message.informativeText() else "")
                + "배포 정보에 다운로드 주소가 없습니다."
            )
        message.addButton("나중에", QMessageBox.ButtonRole.RejectRole)
        message.exec()
        clicked = message.clickedButton()
        if install_button is not None and clicked is install_button:
            self._start_update_download(info)
        elif open_button is not None and clicked is open_button:
            QDesktopServices.openUrl(QUrl(info.download_url))

    def _start_update_download(self, info: UpdateInfo) -> None:
        if self._active_jobs():
            QMessageBox.information(
                self,
                "진행 중인 작업 있음",
                "분석·댓글 등록 등 진행 중인 작업을 먼저 끝낸 뒤 업데이트해 주세요.\n"
                "업데이트 과정에서 앱이 자동으로 종료됩니다.",
            )
            return
        if self._update_download_job is not None:
            return

        destination = installer_download_path(info.latest_version)
        thread = QThread(self)
        worker = UpdateInstallerDownloadWorker(
            info.installer_url,
            destination,
            info.installer_sha256,
            f"SOOPTimeline/{__version__}",
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_update_download_progress)
        worker.succeeded.connect(self._on_update_download_succeeded)
        worker.failed.connect(self._on_update_download_failed)
        worker.cancelled.connect(self._on_update_download_cancelled)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._finish_update_download)
        thread.finished.connect(thread.deleteLater)
        self._downloaded_update_path = ""
        self._update_download_job = (thread, worker)
        self.update_button.setEnabled(False)
        self.status_label.setText(
            f"v{info.latest_version} 업데이트 설치 파일을 다운로드합니다…"
        )
        thread.start()

    @Slot(int, int)
    def _on_update_download_progress(self, received: int, total: int) -> None:
        received_mb = received / (1024 * 1024)
        if total > 0:
            percent = min(100, int(received * 100 / total))
            self.status_label.setText(
                f"업데이트 다운로드 {percent}% · "
                f"{received_mb:,.1f}/{total / (1024 * 1024):,.1f} MB"
            )
        else:
            self.status_label.setText(
                f"업데이트 다운로드 중 · {received_mb:,.1f} MB"
            )

    @Slot(str)
    def _on_update_download_succeeded(self, path: str) -> None:
        self._downloaded_update_path = path
        self.status_label.setText("업데이트 검증을 마쳤습니다. 설치를 시작합니다…")

    @Slot(str)
    def _on_update_download_failed(self, message: str) -> None:
        self.status_label.setText("업데이트 다운로드 또는 검증에 실패했습니다.")
        QMessageBox.warning(
            self,
            "자동 업데이트 실패",
            f"업데이트를 안전하게 준비하지 못했습니다. 기존 앱은 변경되지 않았습니다.\n\n{message}",
        )

    @Slot()
    def _on_update_download_cancelled(self) -> None:
        self.status_label.setText("업데이트 다운로드를 취소했습니다.")

    @Slot()
    def _finish_update_download(self) -> None:
        self._update_download_job = None
        self.update_button.setEnabled(True)
        downloaded_path = self._downloaded_update_path
        self._downloaded_update_path = ""
        if self._quit_after_update_cancel:
            self._quit_after_update_cancel = False
            self._force_quit = True
            QTimer.singleShot(0, self.close)
            return
        if downloaded_path:
            QTimer.singleShot(
                0,
                lambda path=downloaded_path: self._launch_downloaded_update(
                    Path(path)
                ),
            )

    def _launch_downloaded_update(self, path: Path) -> None:
        if not path.is_file():
            self._pending_update_installer_path = ""
            self.update_button.setText("업데이트 확인")
            QMessageBox.warning(
                self,
                "업데이트 파일 없음",
                "다운로드한 업데이트 설치 파일을 찾지 못했습니다. 다시 확인해 주세요.",
            )
            return
        if self._active_jobs():
            self._pending_update_installer_path = str(path)
            self.update_button.setText("다운로드 완료 · 설치")
            self.status_label.setText(
                "업데이트 다운로드 완료 · 진행 중인 작업이 끝나면 설치할 수 있습니다."
            )
            QMessageBox.information(
                self,
                "업데이트 준비 완료",
                "설치 파일 검증을 마쳤습니다. 진행 중인 작업이 끝난 뒤 "
                "‘다운로드 완료 · 설치’를 눌러 주세요.",
            )
            return

        arguments = [
            "/SP-",
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/CLOSEAPPLICATIONS",
            "/CURRENTUSER",
            "/RELAUNCH=1",
            f"/LOG={path.parent / 'update-install.log'}",
        ]
        try:
            subprocess.Popen(
                [str(path), *arguments],
                cwd=str(path.parent),
                close_fds=True,
            )
        except OSError as error:
            self._pending_update_installer_path = str(path)
            self.update_button.setText("다운로드 완료 · 설치")
            QMessageBox.warning(
                self,
                "업데이트 설치 시작 실패",
                "설치 프로그램을 시작하지 못했습니다. 기존 앱은 변경되지 않았습니다.\n\n"
                f"{error}",
            )
            return

        self._pending_update_installer_path = ""
        self._force_quit = True
        self.status_label.setText("업데이트 설치를 위해 앱을 종료합니다…")
        self.close()

    def _offer_gpu_addon_if_needed(self) -> None:
        prompt_state = self.database.get_setting(GPU_ADDON_PROMPT_SETTING, "")
        if prompt_state in {__version__, "never"}:
            return
        try:
            runtime = detect_whisper_runtime("auto")
        except RuntimeError:
            return
        if not runtime.warning:
            return

        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Information)
        message.setWindowTitle("NVIDIA GPU 구성요소")
        message.setText(
            "NVIDIA GPU는 감지됐지만 대용량 CUDA 파일은 기본 앱에서 분리되어 있습니다."
        )
        message.setInformativeText(
            "GPU 구성요소를 한 번 설치하면 이후 일반 앱 업데이트에서는 다시 받을 "
            "필요가 없습니다. 설치하지 않으면 CPU로 계속 사용할 수 있습니다."
        )
        download_button = message.addButton(
            "GPU 구성요소 받기",
            QMessageBox.ButtonRole.AcceptRole,
        )
        message.addButton("CPU로 사용", QMessageBox.ButtonRole.RejectRole)
        message.exec()
        if message.clickedButton() is download_button:
            self.database.set_setting(GPU_ADDON_PROMPT_SETTING, __version__)
            QDesktopServices.openUrl(QUrl(GPU_ADDON_DOWNLOAD_URL))
        else:
            self.database.set_setting(GPU_ADDON_PROMPT_SETTING, "never")

    def _schedule_live_reconnect_retry(self, delay_ms: int) -> None:
        if (
            self._close_after_analysis
            or not self._stale_live_sessions
            or self._live_reconnect_retry_scheduled
        ):
            return
        self._live_reconnect_retry_scheduled = True
        QTimer.singleShot(max(0, delay_ms), self._resume_stale_live_sessions)

    @Slot(str)
    def reconnect_live_session(self, vod_id: str) -> None:
        """Queue a saved live tab for reconnection regardless of its old state."""

        vod = self.database.get_vod(vod_id)
        editor = self._editor_tabs.get(vod_id)
        if vod is None or vod.source_kind != "live" or editor is None:
            return
        if vod_id in self._live_jobs:
            editor.status_label.setText("이미 이 라이브를 실시간으로 분석하고 있습니다.")
            return

        editor.set_live_reconnect_pending(True)
        if (
            self._live_reconnect_target_id == vod_id
            or vod_id in self._stale_live_sessions
        ):
            editor.status_label.setText(
                "이미 라이브 재연결을 준비하고 있습니다. 잠시만 기다려 주세요."
            )
            self._schedule_live_reconnect_retry(0)
            return

        self.database.save_timeline(
            vod_id,
            editor.text(),
            VodState.ANALYZING.value,
        )
        self.database.set_vod_state(vod_id, VodState.ANALYZING.value)
        self._stale_live_sessions.append(vod_id)
        self._live_reconnect_attempts.pop(vod_id, None)
        editor.status_label.setText(
            "저장된 방송 번호로 라이브 재연결을 준비합니다… "
            "기존 자막과 타임라인은 유지됩니다."
        )
        self.status_label.setText(
            f"라이브 수동 재연결 요청 · {vod.streamer_name}"
        )
        self.load_vods()
        self._schedule_live_reconnect_retry(0)

    def _resume_stale_live_sessions(self) -> None:
        """Reconnect an interrupted live session before starting queued VOD work."""

        self._live_reconnect_retry_scheduled = False
        if (
            self._close_after_analysis
            or not self._stale_live_sessions
            or self._live_reconnect_job is not None
        ):
            return
        if self._pretranscribe_jobs:
            # Live audio cannot be recovered later. Let the resumable background
            # VOD transcriptions yield their local FW slots first.
            for thread, _ in self._pretranscribe_jobs.values():
                thread.requestInterruption()
            self.status_label.setText(
                "라이브 자동 재연결을 위해 병렬 FW 자막 추출을 잠시 멈춥니다…"
            )
            self._schedule_live_reconnect_retry(500)
            return
        if (
            self._analysis_jobs
            or self._live_jobs
            or self._style_jobs
            or self._line_rewrite_jobs
            or self._regroup_jobs
            or self._manual_link_job is not None
        ):
            self._schedule_live_reconnect_retry(1_500)
            return

        vod_id = self._stale_live_sessions[0]
        vod = self.database.get_vod(vod_id)
        if (
            vod is None
            or vod.source_kind != "live"
            or vod.state != VodState.ANALYZING.value
        ):
            editor = self._editor_tabs.get(vod_id)
            if editor is not None:
                editor.set_live_reconnect_pending(False)
            self._remove_stale_live_session(vod_id)
            QTimer.singleShot(0, self._resume_analysis_queue_if_idle)
            return

        analyzer = LocalWhisperGeminiAnalyzer.from_database(self.database)
        if not analyzer.available:
            editor = self._editor_tabs.get(vod_id)
            if editor is not None:
                editor.set_live_reconnect_pending(True)
                editor.status_label.setText(
                    "라이브 재연결 대기 · AI 분석 설정을 확인하세요: "
                    f"{analyzer.unavailable_reason}"
                )
            self.status_label.setText(
                "라이브 자동 재연결 대기 · AI 분석 설정을 확인하세요: "
                f"{analyzer.unavailable_reason}"
            )
            self._schedule_live_reconnect_retry(60_000)
            return

        url = vod.url.strip()
        if not url and vod.channel_id:
            suffix = f"/{vod.live_broadcast_no}" if vod.live_broadcast_no else ""
            url = f"https://play.sooplive.com/{vod.channel_id}{suffix}"
        if not url:
            self._finish_unavailable_live_session(
                vod_id,
                "저장된 라이브 방송 주소가 없어 자동 재연결할 수 없습니다.",
            )
            return

        thread = QThread(self)
        thread.setProperty("vod_id", vod_id)
        worker = ManualLinkWorker(url)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.resolved.connect(self._live_reconnect_resolved)
        worker.failed.connect(self._live_reconnect_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._live_reconnect_thread_finished)
        thread.finished.connect(thread.deleteLater)

        self._live_reconnect_target_id = vod_id
        self._live_reconnect_source = None
        self._live_reconnect_error = ""
        self._live_reconnect_attempts[vod_id] = (
            self._live_reconnect_attempts.get(vod_id, 0) + 1
        )
        self._live_reconnect_job = (thread, worker)
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_live_reconnect_pending(True)
            editor.status_label.setText(
                "저장된 방송 번호를 확인하고 라이브에 재연결하는 중… "
                f"시도 {self._live_reconnect_attempts[vod_id]:,}회"
            )
        self.status_label.setText(
            f"중단된 라이브에 자동 재연결 중… · {vod.streamer_name} · "
            f"시도 {self._live_reconnect_attempts[vod_id]:,}회"
        )
        thread.start()

    @Slot(object)
    def _live_reconnect_resolved(self, result: object) -> None:
        if self._live_reconnect_target_id in self._cancelled_live_reconnect_ids:
            return
        vod = self.database.get_vod(self._live_reconnect_target_id)
        if vod is None or not isinstance(result, LiveAudioSource):
            self._live_reconnect_error = (
                "저장된 라이브 방송의 연결 정보를 확인하지 못했습니다."
            )
            return
        expected_broadcast_no = vod.live_broadcast_no.strip()
        if (
            expected_broadcast_no
            and result.broadcast_no != expected_broadcast_no
        ):
            self._live_reconnect_error = (
                "기존 라이브 방송이 종료되었거나 다른 방송으로 전환되었습니다."
            )
            return
        self._live_reconnect_source = result
        self._live_reconnect_error = ""

    @Slot(str)
    def _live_reconnect_failed(self, message: str) -> None:
        if self._live_reconnect_target_id in self._cancelled_live_reconnect_ids:
            return
        self._live_reconnect_error = message.strip() or (
            "SOOP 라이브 자동 재연결에 실패했습니다."
        )

    @Slot()
    def _live_reconnect_thread_finished(self) -> None:
        vod_id = self._live_reconnect_target_id
        source = self._live_reconnect_source
        error = self._live_reconnect_error
        self._live_reconnect_job = None
        self._live_reconnect_target_id = ""
        self._live_reconnect_source = None
        self._live_reconnect_error = ""

        if vod_id in self._cancelled_live_reconnect_ids:
            self._cancelled_live_reconnect_ids.discard(vod_id)
            QTimer.singleShot(0, self._resume_analysis_queue_if_idle)
            return

        if self._close_after_analysis:
            if not self._active_jobs():
                QTimer.singleShot(0, self.close)
            return
        if not vod_id:
            self._schedule_live_reconnect_retry(1_500)
            return

        vod = self.database.get_vod(vod_id)
        if source is not None and vod is not None:
            last_captured = live_capture_position(vod)
            if last_captured <= 0 and vod.duration_text.startswith("시작 "):
                last_captured = float(
                    parse_duration_text(vod.duration_text[3:].strip()) or 0
                )
            self.open_timeline(vod_id)
            started = self.start_live_analysis(
                vod_id,
                source,
                automatic_resume=True,
            )
            if started:
                self._remove_stale_live_session(vod_id)
                self._live_reconnect_attempts.pop(vod_id, None)
                try:
                    missing = record_live_reconnect_gap(
                        vod,
                        last_captured,
                        source.runtime_seconds,
                    )
                except OSError:
                    logger.exception(
                        "Failed to write live reconnect gap log for %s",
                        vod_id,
                    )
                    missing = max(0.0, source.runtime_seconds - last_captured)
                if missing > 1.0:
                    logger.warning(
                        "Live session %s resumed with an uncaptured range "
                        "%s~%s (%ss)",
                        vod_id,
                        format_timestamp(last_captured),
                        format_timestamp(source.runtime_seconds),
                        round(missing, 1),
                    )
                    self.status_label.setText(
                        "라이브 자동 재연결 완료 · 앱이 꺼져 있던 미수신 구간 "
                        f"{format_timestamp(last_captured)}~"
                        f"{format_timestamp(source.runtime_seconds)}은 "
                        "복구 로그에만 기록했습니다."
                    )
                else:
                    self.status_label.setText(
                        "기존 라이브 분석 기록과 자막 캐시에 자동으로 다시 연결했습니다."
                    )
                return

        failure = error or "라이브 분석 작업을 다시 시작하지 못했습니다."
        if self._is_terminal_live_reconnect_error(failure):
            self._finish_unavailable_live_session(vod_id, failure)
            return

        attempt = max(1, self._live_reconnect_attempts.get(vod_id, 1))
        if attempt >= self._MAX_LIVE_RECONNECT_ATTEMPTS:
            self._finish_unavailable_live_session(
                vod_id,
                f"자동 재연결을 {attempt:,}회 시도했지만 복구하지 못했습니다: {failure}",
            )
            return
        retry_index = min(attempt - 1, len(self._LIVE_RECONNECT_RETRY_MS) - 1)
        delay = self._LIVE_RECONNECT_RETRY_MS[retry_index]
        logger.warning(
            "Live auto reconnect failed for %s; retrying in %.1fs: %s",
            vod_id,
            delay / 1_000,
            failure,
        )
        self.status_label.setText(
            f"라이브 자동 재연결 실패 · {delay // 1_000:,}초 뒤 재시도: {failure}"
        )
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_live_reconnect_pending(True)
            editor.status_label.setText(
                f"라이브 재연결 실패 · {delay // 1_000:,}초 뒤 자동 재시도: "
                f"{failure}"
            )
        self._schedule_live_reconnect_retry(delay)

    @staticmethod
    def _is_terminal_live_reconnect_error(message: str) -> bool:
        return any(
            marker in message
            for marker in (
                "종료되었",
                "다른 방송으로 전환",
                "진행 중인 공개 라이브 방송을 찾지 못",
                "현재 공개 라이브 방송을 열 수 없",
                "비밀번호가 필요한",
                "숨김 라이브",
                "연령 확인이 필요한",
                "구독자 전용",
                "저장된 라이브 방송 주소가 없",
            )
        )

    def _finish_unavailable_live_session(
        self,
        vod_id: str,
        message: str,
    ) -> None:
        self._remove_stale_live_session(vod_id)
        document = self.database.get_timeline(vod_id)
        if document is not None:
            self.database.save_timeline(
                vod_id,
                document.text,
                VodState.REVIEW.value,
            )
        self.database.set_vod_state(vod_id, VodState.REVIEW.value)
        self._live_reconnect_attempts.pop(vod_id, None)
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_live_reconnect_pending(False)
            editor.set_live_running(False)
            editor.status_label.setText(
                "현재 방송에는 재연결할 수 없습니다. 저장된 부분은 그대로 "
                f"유지했습니다: {message}"
            )
        self.load_vods()
        self._schedule_replay_link_check(vod_id)
        self.status_label.setText(
            "기존 라이브는 더 이상 연결할 수 없어 저장된 부분만 검수 상태로 "
            f"남겼습니다: {message}"
        )
        logger.info("Live session %s could not be resumed: %s", vod_id, message)
        QTimer.singleShot(0, self._resume_analysis_queue_if_idle)

    def _remove_stale_live_session(self, vod_id: str) -> None:
        self._stale_live_sessions = [
            candidate
            for candidate in self._stale_live_sessions
            if candidate != vod_id
        ]

    def _resume_persisted_analysis(self) -> None:
        if (
            self._analysis_jobs
            or self._live_jobs
            or self._live_reconnect_job is not None
            or self._stale_live_sessions
            or self._style_jobs
            or self._line_rewrite_jobs
            or self._regroup_jobs
            or self._manual_link_job is not None
            or not self._analysis_queue
        ):
            return
        vod_id = self._analysis_queue[0]
        if vod_id in self._pretranscribe_queue:
            QTimer.singleShot(0, self._resume_pretranscribe_if_idle)
            return
        vod = self.database.get_vod(vod_id)
        if vod is None:
            self._analysis_queue.pop(0)
            self.database.remove_analysis_queue(vod_id)
            QTimer.singleShot(0, self._resume_persisted_analysis)
            return
        if vod.source_kind == "live":
            self._analysis_queue.pop(0)
            self.database.remove_analysis_queue(vod_id)
            self.database.set_vod_state(vod_id, VodState.REVIEW.value)
            QTimer.singleShot(0, self._resume_persisted_analysis)
            return
        self.open_timeline(vod_id)
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.status_label.setText(
                "이전 실행에서 중단된 분석을 체크포인트부터 재개합니다…"
            )
        self.start_analysis(vod_id, _from_queue=True)

    def _resume_analysis_queue_if_idle(self) -> None:
        if self._close_after_analysis:
            return
        if self._stale_live_sessions:
            self._schedule_live_reconnect_retry(0)
            return
        if (
            self._live_jobs
            or self._live_reconnect_job is not None
            or self._style_jobs
            or self._line_rewrite_jobs
            or self._regroup_jobs
            or self._manual_link_job is not None
        ):
            return
        analysis_jobs = getattr(self, "_analysis_jobs", {})
        if analysis_jobs:
            ready = getattr(
                self,
                "_analysis_background_fw_ready",
                set(analysis_jobs),
            )
            if not set(analysis_jobs).issubset(ready):
                return
            # A queued VOD can finish its local faster-whisper pass while the
            # current VOD is using Gemini. Gemini work itself stays serialized.
            for queued_vod_id in self._analysis_queue:
                if (
                    queued_vod_id not in self._pretranscribe_attempted_ids
                    and queued_vod_id not in self._pretranscribe_queue
                ):
                    self._pretranscribe_queue.append(queued_vod_id)
            if self._pretranscribe_queue:
                QTimer.singleShot(0, self._resume_pretranscribe_if_idle)
            return
        if self._analysis_queue:
            next_vod_id = self._analysis_queue[0]
            if next_vod_id in self._pretranscribe_queue:
                # Do not let the full worker reach Gemini until its FW-only
                # preparation has completed.
                QTimer.singleShot(0, self._resume_pretranscribe_if_idle)
                return
            QTimer.singleShot(0, self._resume_persisted_analysis)
            if self._pretranscribe_queue:
                QTimer.singleShot(0, self._resume_pretranscribe_if_idle)
        elif self._pretranscribe_queue:
            QTimer.singleShot(0, self._resume_pretranscribe_if_idle)

    # -- Background auto faster-whisper (no Gemini) --------------------------
    def _enqueue_auto_processing(self) -> None:
        """Act on VODs found in the latest discovery run per the auto setting."""
        mode = normalized_auto_analyze_mode(
            self.database.get_setting(AUTO_ANALYZE_SETTING, "off")
        )
        new_ids = self._new_vod_ids_this_run
        self._new_vod_ids_this_run = []
        if mode == "off" or not new_ids:
            return
        targets: list[str] = []
        for vod_id in new_ids:
            vod = self.database.get_vod(vod_id)
            if vod is None or vod.source_kind == "live":
                continue
            targets.append(vod_id)
        if not targets:
            return
        if mode == "full":
            for vod_id in targets:
                if (
                    vod_id in self._analysis_queue
                    or vod_id in self._analysis_jobs
                ):
                    continue
                self._analysis_queue.append(vod_id)
                self.database.enqueue_analysis(vod_id)
                self.database.set_vod_state(vod_id, VodState.QUEUED.value)
                self._sync_editor_analysis_state(vod_id)
            self.load_vods()
            self._resume_analysis_queue_if_idle()
            return
        for vod_id in targets:
            if vod_id not in self._pretranscribe_queue:
                self._pretranscribe_queue.append(vod_id)
        self.status_label.setText(
            f"신규 {len(targets)}개를 백그라운드 자막추출 대기열에 추가했습니다."
        )
        self._resume_pretranscribe_if_idle()

    def _resume_pretranscribe_if_idle(self) -> None:
        if (
            len(self._pretranscribe_jobs)
            >= self._MAX_CONCURRENT_PRETRANSCRIBES
            or not self._pretranscribe_queue
        ):
            return
        if (
            self._close_after_analysis
            or self._stale_live_sessions
            or self._live_reconnect_job is not None
            or self._live_jobs
            or self._style_jobs
            or self._line_rewrite_jobs
            or self._regroup_jobs
            or self._manual_link_job is not None
        ):
            return
        analysis_jobs = getattr(self, "_analysis_jobs", {})
        if analysis_jobs:
            ready = getattr(
                self,
                "_analysis_background_fw_ready",
                set(analysis_jobs),
            )
            if not set(analysis_jobs).issubset(ready):
                return
        available_slots = (
            self._MAX_CONCURRENT_PRETRANSCRIBES
            - len(self._pretranscribe_jobs)
        )
        waiting_vod_ids = [
            vod_id
            for vod_id in self._pretranscribe_queue
            if vod_id not in self._pretranscribe_jobs
        ]
        for vod_id in waiting_vod_ids[:available_slots]:
            self._start_pretranscribe(vod_id)

    def _start_pretranscribe(self, vod_id: str) -> None:
        if vod_id in self._pretranscribe_jobs:
            return
        vod = self.database.get_vod(vod_id)
        if vod is None or vod.source_kind == "live":
            if vod_id in self._pretranscribe_queue:
                self._pretranscribe_queue.remove(vod_id)
            QTimer.singleShot(0, self._resume_pretranscribe_if_idle)
            return
        analyzer = LocalWhisperGeminiAnalyzer.from_database(self.database)
        reusable_live_vods: tuple[Vod, ...] = ()
        if vod.live_broadcast_no:
            reusable_live_vods = tuple(
                self.database.list_live_sessions_for_broadcast(
                    vod.streamer_id,
                    vod.live_broadcast_no,
                )
            )
        thread = QThread(self)
        thread.setProperty("vod_id", vod_id)
        worker = PreTranscribeWorker(analyzer, vod, reusable_live_vods)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress_changed.connect(self._pretranscribe_progress)
        worker.succeeded.connect(self._pretranscribe_succeeded)
        worker.failed.connect(self._pretranscribe_failed)
        worker.cancelled.connect(self._pretranscribe_cancelled)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._pretranscribe_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._pretranscribe_attempted_ids.add(vod_id)
        self._pretranscribe_jobs[vod_id] = (thread, worker)
        self.status_label.setText(
            "FW 자막추출 병렬 실행 "
            f"{len(self._pretranscribe_jobs)}/{self._MAX_CONCURRENT_PRETRANSCRIBES}"
            f" · Gemini 미사용: {vod.title}"
        )
        self.load_vods()
        thread.start()

    def _pretranscribe_progress(
        self,
        vod_id: str,
        percent: int,
        message: str,
    ) -> None:
        self.status_label.setText(
            "FW 자막추출 병렬 실행 "
            f"{len(self._pretranscribe_jobs)}/{self._MAX_CONCURRENT_PRETRANSCRIBES}"
            f" · {percent}% · Gemini 미사용 · {message}"
        )
        if vod_id in self._analysis_queue:
            editor = self._editor_tabs.get(vod_id)
            if editor is not None:
                editor.set_analysis_queued(
                    f"FW 자막추출 {percent}% · 완료 후 Gemini 분석을 시작합니다. · {message}",
                    percent,
                )

    def _pretranscribe_succeeded(self, vod_id: str) -> None:
        if vod_id in self._pretranscribe_queue:
            self._pretranscribe_queue.remove(vod_id)
        self._refresh_editor_cache_state(vod_id)
        remaining = len(self._pretranscribe_queue)
        tail = f" · 대기 {remaining}개" if remaining else ""
        if vod_id in self._analysis_queue:
            self.status_label.setText(
                f"FW 자막추출 완료 · Gemini 분석 순서 대기{tail}"
            )
            editor = self._editor_tabs.get(vod_id)
            if editor is not None:
                editor.set_analysis_queued(
                    f"FW 자막추출 완료 · Gemini 분석 순서 대기{tail}"
                )
        else:
            self.status_label.setText(f"백그라운드 FW 자막추출 완료{tail}")

    def _pretranscribe_failed(self, vod_id: str, message: str) -> None:
        if vod_id in self._pretranscribe_queue:
            self._pretranscribe_queue.remove(vod_id)
        logger.warning("Auto transcribe failed for %s: %s", vod_id, message)

    def _pretranscribe_cancelled(self, vod_id: str) -> None:
        # Interrupted (usually by a manual job). Leave it at the front of the
        # queue so it resumes from its partial capture once things are idle.
        self._pretranscribe_attempted_ids.discard(vod_id)

    def _pretranscribe_thread_finished(self) -> None:
        thread = self.sender()
        vod_id = str(thread.property("vod_id") or "") if thread is not None else ""
        if vod_id:
            self._pretranscribe_jobs.pop(vod_id, None)
        if self._close_after_analysis:
            if not self._active_jobs():
                QTimer.singleShot(0, self.close)
            return
        # Resumes a queued manual analysis first, else the next pre-transcribe.
        QTimer.singleShot(0, self._resume_analysis_queue_if_idle)

    def reanalyze_live_as_vod(self, live_vod_id: str) -> None:
        """Analyze a finished live broadcast from its merged replay item."""
        if self._active_jobs():
            QMessageBox.information(
                self,
                "AI 작업 진행 중",
                self._ai_busy_message(
                    "이 작업이 끝난 뒤 다시보기 전체 분석을 시작하세요."
                ),
            )
            return
        live_vod = self.database.get_vod(live_vod_id)
        if live_vod is None or live_vod.source_kind != "live":
            return

        replay = self._linked_replay_for(live_vod)
        if replay is not None:
            answer = QMessageBox.question(
                self,
                "연결된 다시보기로 전체 재분석",
                f"'{replay.title}' 다시보기 전체 범위를 완성할까요?\n\n"
                "같은 방송 번호로 확인된 라이브 자막은 그대로 재사용하고, "
                "앱이 꺼져 있었던 구간 등 실제 누락 부분만 새로 분석합니다.\n\n"
                "현재 라이브 타임라인은 다시보기 항목과 버전 기록에 보존되고, "
                "라이브 탭은 다시보기 탭으로 바뀝니다.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._start_linked_replay_reanalysis(
                    live_vod_id,
                    replay.vod_id,
                )
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
        try:
            parsed = parse_soop_link(link)
        except ValueError as error:
            QMessageBox.information(self, "링크 확인", str(error))
            return
        if parsed.kind != "vod":
            QMessageBox.information(
                self,
                "다시보기 주소 필요",
                "라이브 주소가 아니라 vod.sooplive.com 다시보기 주소를 입력하세요.",
            )
            return
        self._pending_reanalysis_live_id = live_vod_id
        self.manual_link_input.setText(link)
        self.resolve_manual_link()

    def _start_linked_replay_reanalysis(
        self,
        live_vod_id: str,
        replay_vod_id: str,
    ) -> None:
        if self._active_jobs():
            QMessageBox.information(
                self,
                "AI 작업 진행 중",
                self._ai_busy_message(
                    "이 작업이 끝난 뒤 다시보기 전체 분석을 시작하세요."
                ),
            )
            return
        live_vod = self.database.get_vod(live_vod_id)
        replay = self.database.get_vod(replay_vod_id)
        if (
            live_vod is None
            or live_vod.source_kind != "live"
            or replay is None
            or replay.source_kind == "live"
            or live_vod.linked_vod_id != replay.vod_id
        ):
            QMessageBox.critical(
                self,
                "다시보기 연결 오류",
                "라이브 기록에 연결된 실제 다시보기를 확인하지 못했습니다.",
            )
            return
        reusable_live_vods = self.database.list_live_sessions_for_broadcast(
            live_vod.streamer_id,
            live_vod.live_broadcast_no,
        )
        if not reusable_live_vods:
            reusable_live_vods = [live_vod]
        self._apply_linked_replay(live_vod_id, replay_vod_id)
        self.open_timeline(replay_vod_id)
        editor = self._editor_tabs.get(replay_vod_id)
        if editor is None:
            return
        self.start_analysis(
            replay.vod_id,
            revision_reason="전체 다시보기 재분석 전 · 라이브 분석본",
            reusable_live_vods=tuple(reusable_live_vods),
        )

    def start_analysis(
        self,
        vod_id: str,
        *,
        _from_queue: bool = False,
        target_vod_id: str | None = None,
        revision_reason: str = "AI 분석 전",
        reusable_live_vods: tuple[Vod, ...] = (),
    ) -> None:
        if getattr(self, "_close_after_analysis", False):
            return
        source_vod_id = vod_id
        target_vod_id = target_vod_id or source_vod_id
        targeted = target_vod_id != source_vod_id
        source_vod = self.database.get_vod(source_vod_id)
        target_vod = self.database.get_vod(target_vod_id)
        editor = self._editor_tabs.get(target_vod_id)
        if source_vod is None or target_vod is None or editor is None:
            return
        if (
            not reusable_live_vods
            and source_vod.source_kind != "live"
            and getattr(source_vod, "live_broadcast_no", "")
        ):
            reusable_live_vods = tuple(
                self.database.list_live_sessions_for_broadcast(
                    source_vod.streamer_id,
                    source_vod.live_broadcast_no,
                )
            )
        if self._live_reconnect_job is not None or self._stale_live_sessions:
            editor.status_label.setText(
                "기존 라이브 자동 재연결을 먼저 처리하고 있습니다."
            )
            self._schedule_live_reconnect_retry(0)
            return
        if source_vod.source_kind == "live":
            if editor is not None:
                editor.status_label.setText(
                    "라이브 세션은 수동 링크 입력창에 방송 링크를 다시 넣어 시작하세요."
                )
            return
        if self._live_jobs:
            if editor is not None:
                editor.status_label.setText(
                    self._ai_busy_message(
                        "이 작업이 끝난 뒤 다시보기 분석을 시작할 수 있습니다."
                    )
                )
            return
        if self._style_jobs:
            if editor is not None:
                editor.status_label.setText(
                    self._ai_busy_message(
                        "이 작업이 끝난 뒤 다시보기 분석을 시작할 수 있습니다."
                    )
                )
            return
        if self._line_rewrite_jobs:
            if editor is not None:
                editor.status_label.setText(
                    self._ai_busy_message(
                        "이 작업이 끝난 뒤 다시보기 분석을 시작할 수 있습니다."
                    )
                )
            return
        if self._regroup_jobs:
            if editor is not None:
                editor.status_label.setText(
                    self._ai_busy_message(
                        "이 작업이 끝난 뒤 다시보기 분석을 시작할 수 있습니다."
                    )
                )
            return
        if target_vod_id in self._analysis_jobs or (
            not targeted
            and source_vod_id in self._analysis_queue
            and not _from_queue
        ):
            if editor is not None:
                editor.status_label.setText("이미 분석 중이거나 대기열에 있습니다.")
            return

        if self._analysis_jobs:
            running_target_id = next(iter(self._analysis_jobs))
            running_source_id = self._analysis_source_ids.get(
                running_target_id,
                running_target_id,
            )
            running_vod = self.database.get_vod(running_source_id)
            running_title = (
                running_vod.title if running_vod else running_source_id
            )
            if targeted:
                editor.status_label.setText(
                    f"'{running_title}' 분석이 끝난 뒤 전체 재분석을 다시 눌러주세요."
                )
                return
            self._analysis_queue.append(source_vod_id)
            self.database.enqueue_analysis(source_vod_id)
            self.database.set_vod_state(
                source_vod_id,
                VodState.QUEUED.value,
            )
            if source_vod_id not in self._pretranscribe_queue:
                self._pretranscribe_queue.append(source_vod_id)
            if editor is not None:
                editor.set_analysis_queued(
                    f"FW 자막추출 중/대기 · Gemini 정리는 '{running_title}' 완료 후 시작"
                )
            self.status_label.setText(
                "새 분석 요청의 FW 자막추출을 병렬로 실행합니다 · "
                f"Gemini 대기 {len(self._analysis_queue)}개"
            )
            self.load_vods()
            self._resume_pretranscribe_if_idle()
            return

        if (
            self._pretranscribe_jobs
            and source_vod_id in self._pretranscribe_jobs
        ):
            if targeted:
                editor.status_label.setText(
                    "백그라운드 자막추출이 끝난 뒤 전체 다시보기 분석을 다시 눌러주세요."
                )
                return
            if source_vod_id not in self._analysis_queue:
                self._analysis_queue.append(source_vod_id)
                self.database.enqueue_analysis(source_vod_id)
                self.database.set_vod_state(
                    source_vod_id,
                    VodState.QUEUED.value,
                )
            editor.set_analysis_queued(
                "FW 자막추출 중 · 완료 후 Gemini 분석을 시작합니다."
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

        if _from_queue and source_vod_id in self._analysis_queue:
            self._analysis_queue.remove(source_vod_id)
        if source_vod_id in self._pretranscribe_queue:
            self._pretranscribe_queue.remove(source_vod_id)

        self.database.create_timeline_revision(
            target_vod_id,
            editor.text(),
            revision_reason,
        )

        thread = QThread(self)
        thread.setProperty("vod_id", target_vod_id)
        worker = AnalysisWorker(
            self.analyzer,
            source_vod,
            result_vod_id=target_vod_id,
            reusable_live_vods=reusable_live_vods,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress_changed.connect(self._analysis_progress_changed)
        worker.preview_changed.connect(editor.set_analysis_preview)
        worker.usage_changed.connect(editor.set_ai_usage)
        worker.succeeded.connect(self._analysis_succeeded)
        worker.failed.connect(self._analysis_failed)
        worker.cancelled.connect(self._analysis_cancelled)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._analysis_thread_finished)
        thread.finished.connect(thread.deleteLater)

        self._analysis_jobs[target_vod_id] = (thread, worker)
        self._analysis_source_ids[target_vod_id] = source_vod_id
        self._analysis_previous_states[target_vod_id] = (
            source_vod.state,
            target_vod.state,
        )
        if targeted:
            self.database.set_vod_state(
                target_vod_id,
                VodState.ANALYZING.value,
            )
        else:
            self.database.mark_analysis_running(source_vod_id)
            self.database.set_vod_state(
                source_vod_id,
                VodState.ANALYZING.value,
            )
        editor.set_analysis_running(True)
        duration_seconds = parse_duration_text(source_vod.duration_text)
        estimate = estimate_timeline_calls(duration_seconds or 0)
        editor.set_analysis_progress(
            0,
            (
                "다시보기 전체 범위 준비 · 기존 라이브 자막과 겹치는 구간은 "
                "건너뜁니다 · "
                if targeted
                else "SOOP 고속 오디오 분석 준비 · "
            )
            + f"AI 호출 예상 약 {estimate:,}회 "
            "(자막 구간 수에 따라 달라질 수 있음)",
        )
        self.status_label.setText(
            f"전체 다시보기 분석 시작: {source_vod.title}"
            if targeted
            else f"AI 분석 시작: {source_vod.title}"
        )
        self.load_vods()
        thread.start()
        if self._analysis_queue or self._pretranscribe_queue:
            QTimer.singleShot(0, self._resume_analysis_queue_if_idle)

    @Slot(str, int, str)
    def _analysis_progress_changed(
        self,
        target_vod_id: str,
        percent: int,
        message: str,
    ) -> None:
        editor = self._editor_tabs.get(target_vod_id)
        if editor is not None:
            editor.set_analysis_progress(percent, message)
        # The analyzer maps local transcription below 80%; from 80% onward it
        # is waiting on Gemini, so the single Whisper backend is available for
        # one background preparation job.
        if (
            percent >= 80
            and target_vod_id in self._analysis_jobs
            and target_vod_id not in self._analysis_background_fw_ready
        ):
            self._analysis_background_fw_ready.add(target_vod_id)
            QTimer.singleShot(0, self._resume_analysis_queue_if_idle)

    def start_live_analysis(
        self,
        vod_id: str,
        source: LiveAudioSource,
        *,
        automatic_resume: bool = False,
    ) -> bool:
        if getattr(self, "_close_after_analysis", False):
            return False
        if (
            self._analysis_jobs
            or (self._analysis_queue and not automatic_resume)
            or self._style_jobs
            or self._line_rewrite_jobs
            or self._live_jobs
            or self._regroup_jobs
            or self._pretranscribe_jobs
        ):
            message = self._ai_busy_message(
                "이 작업이 끝난 뒤 라이브 분석을 시작할 수 있습니다."
            )
            if automatic_resume:
                logger.info("Deferred live auto resume for %s: %s", vod_id, message)
            else:
                self._manual_link_failed(message)
            return False
        vod = self.database.get_vod(vod_id)
        editor = self._editor_tabs.get(vod_id)
        analyzer = LocalWhisperGeminiAnalyzer.from_database(self.database)
        if vod is None or editor is None:
            return False
        if not analyzer.available:
            if automatic_resume:
                self.status_label.setText(
                    f"라이브 자동 재연결 대기: {analyzer.unavailable_reason}"
                )
            else:
                self._manual_link_failed(analyzer.unavailable_reason)
            return False

        thread = QThread(self)
        thread.setProperty("vod_id", vod_id)
        worker = LiveAnalysisWorker(
            analyzer,
            vod,
            source,
            resume_document=editor.text(),
        )
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
        self._live_shutdown_resume_ids.discard(vod_id)
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
        return True

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
        self._live_shutdown_resume_ids.discard(vod_id)
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.apply_live_result(document)
        self.database.save_timeline(vod_id, document, VodState.REVIEW.value)
        self.database.set_vod_state(vod_id, VodState.REVIEW.value)
        self._save_review_feedback_draft(vod_id, document)
        self.status_label.setText(
            "라이브 수신과 AI 최종 타임라인 정리가 완료되었습니다."
        )
        self._refresh_editor_cache_state(vod_id)
        self.load_vods()
        self._schedule_replay_link_check(vod_id)

    def _preserve_live_for_restart(
        self,
        vod_id: str,
        reason: str,
    ) -> None:
        editor = self._editor_tabs.get(vod_id)
        document = self.database.get_timeline(vod_id)
        text = document.text if document is not None else ""
        if editor is not None:
            editor.set_live_running(False)
            editor.status_label.setText(
                "앱 종료 후 같은 라이브에 자동 재연결하도록 현재 기록을 저장했습니다."
            )
            text = editor.text()
        self.database.save_timeline(
            vod_id,
            text,
            VodState.ANALYZING.value,
        )
        self.database.set_vod_state(vod_id, VodState.ANALYZING.value)
        logger.info(
            "Live session %s paused for automatic restart recovery: %s",
            vod_id,
            reason,
        )

    @Slot(str, str)
    def _live_failed(self, vod_id: str, message: str) -> None:
        if vod_id in self._live_shutdown_resume_ids:
            self._preserve_live_for_restart(vod_id, message)
            return
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
        self._schedule_replay_link_check(vod_id)
        QMessageBox.critical(self, "라이브 분석 실패", message)

    @Slot(str)
    def _live_cancelled(self, vod_id: str) -> None:
        if vod_id in self._live_shutdown_resume_ids:
            self._preserve_live_for_restart(vod_id, "application shutdown")
            return
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_live_running(False)
            editor.status_label.setText("라이브 분석을 중단했습니다.")
            self.database.save_timeline(
                vod_id,
                editor.text(),
                VodState.REVIEW.value,
            )
        else:
            document = self.database.get_timeline(vod_id)
            if document is not None:
                self.database.save_timeline(
                    vod_id,
                    document.text,
                    VodState.REVIEW.value,
                )
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
        else:
            self._resume_analysis_queue_if_idle()

    def start_style_correction(self, vod_id: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is None:
            return
        if (
            self._analysis_jobs
            or self._analysis_queue
            or self._live_jobs
            or self._line_rewrite_jobs
        ):
            editor.status_label.setText(
                self._ai_busy_message(
                    "이 작업이 끝난 뒤 문체 교정을 시작할 수 있습니다."
                )
            )
            return
        if self._regroup_jobs:
            editor.status_label.setText(
                self._ai_busy_message(
                    "이 작업이 끝난 뒤 문체 교정을 시작할 수 있습니다."
                )
            )
            return
        if self._style_jobs:
            editor.status_label.setText(
                self._ai_busy_message(
                    "이 작업이 끝난 뒤 다른 문체 교정을 시작할 수 있습니다."
                )
            )
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
        vod = self.database.get_vod(vod_id)
        title = vod.title if vod is not None else vod_id
        self.status_label.setText(f"AI 문체 교정 시작: {title}")
        thread.start()

    @Slot(str, str)
    def _style_succeeded(self, vod_id: str, document: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.apply_style_result(document)
            editor.set_style_running(False)
        self.database.save_timeline(vod_id, document, VodState.REVIEW.value)
        self.database.set_vod_state(vod_id, VodState.REVIEW.value)
        self._save_review_feedback_draft(vod_id, document)
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
        if self._close_after_analysis and not self._active_jobs():
            QTimer.singleShot(0, self.close)
        else:
            self._resume_analysis_queue_if_idle()

    @Slot(str, str, str, int, int)
    def start_line_rewrite(
        self,
        vod_id: str,
        mode: str,
        line: str,
        next_seconds: int,
        line_start: int,
    ) -> None:
        editor = self._editor_tabs.get(vod_id)
        vod = self.database.get_vod(vod_id)
        if editor is None or vod is None:
            return
        if vod_id in self._line_rewrite_jobs:
            editor.status_label.setText("이미 이 탭에서 줄 변환이 진행 중입니다.")
            return
        if (
            self._analysis_jobs
            or self._analysis_queue
            or self._live_jobs
            or self._regroup_jobs
            or self._style_jobs
            or self._line_rewrite_jobs
        ):
            editor.status_label.setText(
                self._ai_busy_message(
                    "이 작업이 끝난 뒤 한 줄 AI 변환을 시작할 수 있습니다."
                )
            )
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

        transcript = load_cached_transcript(self._cache_source_vod(vod))
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
            line_start,
            transcript,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._line_rewrite_succeeded)
        worker.usage_changed.connect(self._ai_usage_changed)
        worker.failed.connect(self._line_rewrite_failed)
        worker.cancelled.connect(self._line_rewrite_cancelled)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._line_rewrite_thread_finished)
        thread.finished.connect(thread.deleteLater)

        self._line_rewrite_jobs[vod_id] = (thread, worker)
        editor.set_line_rewrite_running(True, mode)
        self.status_label.setText(f"한 줄 AI 변환 시작: {vod.title}")
        thread.start()

    @Slot(str, str, str, int)
    def _line_rewrite_succeeded(
        self,
        vod_id: str,
        original_line: str,
        new_line: str,
        line_start: int,
    ) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is None:
            return
        editor.set_line_rewrite_running(False)
        editor.apply_line_rewrite(original_line, new_line, line_start)
        self._save_review_feedback_draft(vod_id, editor.text())

    @Slot(str, str)
    def _line_rewrite_failed(self, vod_id: str, message: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_line_rewrite_running(False)
            editor.status_label.setText(f"줄 변환 실패: {message}")

    @Slot(str)
    def _line_rewrite_cancelled(self, vod_id: str) -> None:
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_line_rewrite_running(False)
            editor.status_label.setText("줄 변환을 취소했습니다.")

    @Slot()
    def _line_rewrite_thread_finished(self) -> None:
        thread = self.sender()
        vod_id = str(thread.property("vod_id") or "") if thread is not None else ""
        if not vod_id:
            return
        self._line_rewrite_jobs.pop(vod_id, None)
        if self._close_after_analysis and not self._active_jobs():
            QTimer.singleShot(0, self.close)
        else:
            self._resume_analysis_queue_if_idle()

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
            or self._line_rewrite_jobs
            or self._regroup_jobs
        ):
            editor.status_label.setText(
                self._ai_busy_message(
                    "이 작업이 끝난 뒤 주제 다시 묶기를 시작할 수 있습니다."
                )
            )
            return
        analyzer = LocalWhisperGeminiAnalyzer.from_database(self.database)
        if not analyzer.available:
            QMessageBox.information(self, "AI 설정 필요", analyzer.unavailable_reason)
            return
        cache_vod = self._cache_source_vod(vod)

        self.database.create_timeline_revision(
            vod_id,
            editor.text(),
            f"주제 다시 묶기 전 ({granularity})",
        )
        thread = QThread(self)
        thread.setProperty("vod_id", vod_id)
        worker = TimelineRegroupWorker(
            analyzer,
            cache_vod,
            granularity,
            result_vod_id=vod_id,
        )
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
        self.status_label.setText(f"주제 다시 묶기 시작: {vod.title}")
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
        self._save_review_feedback_draft(vod_id, document)
        self._refresh_editor_cache_state(vod_id)
        vod = self.database.get_vod(vod_id)
        cache_vod_id = (
            self._cache_source_vod(vod).vod_id
            if vod is not None
            else vod_id
        )
        if has_pending_timeline_finalization(cache_vod_id):
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
        else:
            self._resume_analysis_queue_if_idle()

    def cancel_analysis(self, vod_id: str) -> None:
        live_job = self._live_jobs.get(vod_id)
        if live_job is not None:
            thread, worker = live_job
            worker.request_stop(finalize=True)
            thread.requestInterruption()
            editor = self._editor_tabs.get(vod_id)
            if editor is not None:
                editor.request_live_stop()
            self.status_label.setText(
                "라이브 종료 요청됨 · 남은 자막과 AI 최종 타임라인을 정리합니다…"
            )
            return
        target_vod_id = self._active_analysis_target_id(vod_id)
        job = (
            self._analysis_jobs.get(target_vod_id)
            if target_vod_id is not None
            else None
        )
        if job is None:
            return
        thread, _ = job
        thread.requestInterruption()
        editor = self._editor_tabs.get(target_vod_id)
        if editor is not None:
            editor.set_analysis_progress(
                editor.analysis_progress.value(),
                "취소를 요청했습니다. 현재 처리 구간이 끝날 때까지 기다려주세요…",
            )

    @Slot(str, str)
    def _analysis_succeeded(self, vod_id: str, document: str) -> None:
        source_vod_id = self._analysis_source_ids.get(vod_id, vod_id)
        targeted = source_vod_id != vod_id
        editor = self._editor_tabs.get(vod_id)
        if targeted:
            source_editor = self._editor_tabs.get(source_vod_id)
            source_document = self.database.get_timeline(source_vod_id)
            source_text = (
                source_editor.text()
                if source_editor is not None
                else (source_document.text if source_document is not None else "")
            )
            if source_text.strip():
                self.database.create_timeline_revision(
                    source_vod_id,
                    source_text,
                    "다시보기 전체 재분석 전",
                )
            live_text = editor.text() if editor is not None else ""
            if live_text.strip():
                self.database.create_timeline_revision(
                    source_vod_id,
                    live_text,
                    "라이브 분석본 · 전체 다시보기 재분석 전",
                )
            self.database.save_timeline(
                source_vod_id,
                document,
                VodState.REVIEW.value,
            )
            self.database.set_vod_state(
                source_vod_id,
                VodState.REVIEW.value,
            )
            _, target_state = self._analysis_previous_states.get(
                vod_id,
                (VodState.NEW.value, VodState.REVIEW.value),
            )
            self.database.set_vod_state(vod_id, target_state)
            editor = self._replace_live_tab_with_replay(
                vod_id,
                source_vod_id,
                document,
            )
        else:
            if editor is not None:
                editor.apply_analysis_result(document)
                editor.set_analysis_running(False)
            self.database.save_timeline(
                vod_id,
                document,
                VodState.REVIEW.value,
            )
            self.database.set_vod_state(vod_id, VodState.REVIEW.value)
        self._save_review_feedback_draft(source_vod_id, document)
        self.database.remove_analysis_queue(source_vod_id)
        if targeted:
            self.database.remove_analysis_queue(vod_id)
            self._refresh_editor_cache_state(source_vod_id)
        else:
            self._refresh_editor_cache_state(vod_id)
        if has_pending_timeline_finalization(source_vod_id):
            self.status_label.setText(
                "구간별 임시 타임라인을 저장했습니다. Gemini 한도 복구 후 최종 정리를 재시도하세요."
            )
            if editor is not None:
                editor.status_label.setText(self.status_label.text())
        else:
            self.status_label.setText(
                "전체 다시보기 분석을 기존 라이브 탭에 반영했습니다. 결과를 검수하세요."
                if targeted
                else "AI 타임라인 생성이 완료되었습니다. 결과를 검수하세요."
            )
        self.load_vods()

    @Slot(str, str)
    def _analysis_failed(self, vod_id: str, message: str) -> None:
        source_vod_id = self._analysis_source_ids.get(vod_id, vod_id)
        targeted = source_vod_id != vod_id
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_analysis_running(False)
            editor.status_label.setText(
                f"전체 다시보기 분석 실패 · 기존 라이브 타임라인은 유지됨: {message}"
                if targeted
                else f"분석 실패: {message}"
            )
        if targeted:
            source_state, target_state = self._analysis_previous_states.get(
                vod_id,
                (VodState.NEW.value, VodState.REVIEW.value),
            )
            self.database.set_vod_state(source_vod_id, source_state)
            self.database.set_vod_state(vod_id, target_state)
        else:
            source_state, _ = self._analysis_previous_states.get(
                vod_id,
                (VodState.REVIEW.value, VodState.REVIEW.value),
            )
            restored_state = (
                source_state
                if source_state
                in {
                    VodState.REVIEW.value,
                    VodState.READY.value,
                    VodState.COPIED.value,
                    VodState.PUBLISHED.value,
                    VodState.SKIPPED.value,
                }
                else VodState.FAILED.value
            )
            self.database.set_vod_state(vod_id, restored_state)
        self.database.remove_analysis_queue(source_vod_id)
        if targeted:
            self.database.remove_analysis_queue(vod_id)
        self._refresh_editor_cache_state(vod_id)
        self.status_label.setText(
            "전체 다시보기 분석에 실패했습니다. 기존 라이브 타임라인은 그대로 유지됩니다."
            if targeted
            else "AI 분석에 실패했습니다."
        )
        self.load_vods()
        QMessageBox.critical(self, "AI 분석 실패", message)

    @Slot(str)
    def _analysis_cancelled(self, vod_id: str) -> None:
        source_vod_id = self._analysis_source_ids.get(vod_id, vod_id)
        targeted = source_vod_id != vod_id
        editor = self._editor_tabs.get(vod_id)
        if editor is not None:
            editor.set_analysis_running(False)
            editor.status_label.setText(
                "전체 다시보기 분석을 취소했습니다. 기존 라이브 타임라인은 유지됩니다."
                if targeted
                else "분석을 취소했습니다."
            )
        if targeted:
            source_state, target_state = self._analysis_previous_states.get(
                vod_id,
                (VodState.NEW.value, VodState.REVIEW.value),
            )
            self.database.set_vod_state(source_vod_id, source_state)
            self.database.set_vod_state(vod_id, target_state)
        else:
            source_state, _ = self._analysis_previous_states.get(
                vod_id,
                (VodState.REVIEW.value, VodState.REVIEW.value),
            )
            restored_state = (
                VodState.REVIEW.value
                if source_state in {VodState.ANALYZING.value, VodState.QUEUED.value}
                else source_state
            )
            self.database.set_vod_state(vod_id, restored_state)
        self.database.remove_analysis_queue(source_vod_id)
        if targeted:
            self.database.remove_analysis_queue(vod_id)
        self._refresh_editor_cache_state(vod_id)
        self.status_label.setText(
            "전체 다시보기 분석을 취소했습니다. 기존 결과는 유지됩니다."
            if targeted
            else "AI 분석을 취소했습니다."
        )
        self.load_vods()

    @Slot()
    def _analysis_thread_finished(self) -> None:
        thread = self.sender()
        vod_id = str(thread.property("vod_id") or "") if thread is not None else ""
        if not vod_id:
            return
        self._analysis_jobs.pop(vod_id, None)
        self._analysis_background_fw_ready.discard(vod_id)
        self._analysis_source_ids.pop(vod_id, None)
        self._analysis_previous_states.pop(vod_id, None)
        if self._close_after_analysis:
            self._analysis_queue.clear()
            if not self._active_jobs():
                QTimer.singleShot(0, self.close)
            return
        self._resume_analysis_queue_if_idle()

    def _current_ai_job_label(
        self,
        preferred_vod_id: str | None = None,
    ) -> str:
        candidates: list[tuple[str, set[str], str]] = []

        for target_vod_id in self._analysis_jobs:
            source_vod_id = self._analysis_source_ids.get(
                target_vod_id,
                target_vod_id,
            )
            candidates.append(
                (
                    "다시보기 분석",
                    {target_vod_id, source_vod_id},
                    source_vod_id,
                )
            )
        for vod_id in self._live_jobs:
            candidates.append(("라이브 실시간 분석", {vod_id}, vod_id))
        for vod_id in self._style_jobs:
            candidates.append(("AI 문체 교정", {vod_id}, vod_id))
        for vod_id in self._line_rewrite_jobs:
            candidates.append(("한 줄 AI 변환", {vod_id}, vod_id))
        for vod_id in self._regroup_jobs:
            candidates.append(("주제 다시 묶기", {vod_id}, vod_id))

        if preferred_vod_id:
            preferred = [
                candidate
                for candidate in candidates
                if preferred_vod_id in candidate[1]
            ]
            if preferred:
                candidates = preferred

        if candidates:
            job_name, _, display_vod_id = candidates[0]
            return (
                f"{job_name} · "
                f"'{self._vod_job_title(display_vod_id)}'"
            )

        if preferred_vod_id and preferred_vod_id in self._analysis_queue:
            return (
                "Gemini 분석 대기 · "
                f"'{self._vod_job_title(preferred_vod_id)}'"
            )
        if self._analysis_queue:
            vod_id = self._analysis_queue[0]
            return f"Gemini 분석 대기 · '{self._vod_job_title(vod_id)}'"

        pretranscribe_ids = list(self._pretranscribe_jobs)
        if preferred_vod_id and preferred_vod_id in pretranscribe_ids:
            pretranscribe_ids = [preferred_vod_id]
        if pretranscribe_ids:
            first_vod_id = pretranscribe_ids[0]
            extra = len(pretranscribe_ids) - 1
            suffix = f" 외 {extra}개" if extra else ""
            return (
                "FW 자막추출(Gemini 미사용) · "
                f"'{self._vod_job_title(first_vod_id)}'{suffix}"
            )
        return ""

    def _vod_job_title(self, vod_id: str) -> str:
        vod = self.database.get_vod(vod_id)
        title = str(vod.title if vod is not None else vod_id).strip() or vod_id
        return title if len(title) <= 80 else f"{title[:79]}…"

    def _ai_busy_message(
        self,
        instruction: str,
        *,
        preferred_vod_id: str | None = None,
    ) -> str:
        current = self._current_ai_job_label(preferred_vod_id)
        if current:
            return f"현재 AI 작업: {current}\n{instruction}"
        return f"다른 AI 작업이 진행 중입니다.\n{instruction}"

    def _vod_active_job(self, vod_id: str) -> bool:
        return (
            vod_id in self._analysis_jobs
            or vod_id in self._analysis_source_ids.values()
            or vod_id in self._analysis_queue
            or vod_id in self._live_jobs
            or vod_id in self._pretranscribe_jobs
            or vod_id in self._pretranscribe_queue
            or vod_id in self._stale_live_sessions
            or self._live_reconnect_target_id == vod_id
            or self._active_auxiliary_ai_job(vod_id) is not None
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
        open_action = menu.addAction("타임라인 작업창 열기")
        open_action.triggered.connect(
            lambda _=False, vid=vod_id: self.open_timeline(vid)
        )
        source_action = menu.addAction("SOOP에서 열기")
        source_action.triggered.connect(
            lambda _=False, url=vod.url: QDesktopServices.openUrl(QUrl(url))
        )
        if self._vod_active_job(vod_id):
            cancel_action = menu.addAction("AI 작업 취소")
            cancel_action.triggered.connect(
                lambda _=False, vid=vod_id: self._cancel_or_dequeue(vid)
            )
        menu.addSeparator()
        visibility_action = menu.addAction(
            "목록에 다시 표시" if vod.hidden else "다시보기 목록에서 숨기기"
        )
        visibility_action.setEnabled(not self._vod_active_job(vod_id))
        visibility_action.triggered.connect(
            lambda _=False, vid=vod_id, hidden=not vod.hidden: self._set_vod_hidden(
                vid,
                hidden,
            )
        )
        menu.exec(self.vod_table.viewport().mapToGlobal(pos))

    def _set_vod_hidden(self, vod_id: str, hidden: bool) -> None:
        if self._vod_active_job(vod_id):
            QMessageBox.information(
                self,
                "작업 진행 중",
                "진행 중인 AI 작업을 마친 뒤 목록에서 숨길 수 있습니다.",
            )
            return
        if hidden:
            answer = QMessageBox.question(
                self,
                "다시보기 목록에서 숨기기",
                "이 영상을 목록에서 숨길까요?\n\n"
                "타임라인, 메모, 저장 자막은 삭제되지 않으며 "
                "‘숨긴 영상’ 필터에서 언제든 다시 표시할 수 있습니다.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.database.set_vod_hidden(vod_id, hidden)
        self.load_vods()
        self.status_label.setText(
            "다시보기 목록에서 숨겼습니다."
            if hidden
            else "숨긴 영상을 목록에 다시 표시했습니다."
        )

    def _cancel_or_dequeue(self, vod_id: str) -> None:
        if (
            self._active_analysis_target_id(vod_id) is not None
            or vod_id in self._live_jobs
        ):
            self.cancel_analysis(vod_id)
            return
        if (
            vod_id in getattr(self, "_stale_live_sessions", ())
            or getattr(self, "_live_reconnect_target_id", "") == vod_id
        ):
            self._cancelled_live_reconnect_ids.add(vod_id)
            if (
                getattr(self, "_live_reconnect_target_id", "") == vod_id
                and getattr(self, "_live_reconnect_job", None)
            ):
                self._live_reconnect_job[0].requestInterruption()
            self._finish_unavailable_live_session(
                vod_id,
                "사용자가 라이브 재연결을 취소했습니다.",
            )
            return
        if vod_id in self._analysis_queue:
            self._analysis_queue.remove(vod_id)
            self.database.remove_analysis_queue(vod_id)
            if vod_id in self._pretranscribe_queue:
                self._pretranscribe_queue.remove(vod_id)
                active_pretranscribe = self._pretranscribe_jobs.get(vod_id)
                if active_pretranscribe is not None:
                    active_pretranscribe[0].requestInterruption()
            self.database.set_vod_state(vod_id, VodState.REVIEW.value)
            editor = self._editor_tabs.get(vod_id)
            if editor is not None:
                editor.set_analysis_running(False)
                editor.status_label.setText(
                    "FW 자막추출 및 Gemini 분석 대기를 취소했습니다."
                )
            self.status_label.setText(
                "FW 자막추출과 분석 대기열에서 제거했습니다."
            )
            self.load_vods()
            return
        pretranscribe_queue = getattr(self, "_pretranscribe_queue", [])
        pretranscribe_jobs = getattr(self, "_pretranscribe_jobs", {})
        if vod_id in pretranscribe_queue or vod_id in pretranscribe_jobs:
            if vod_id in pretranscribe_queue:
                pretranscribe_queue.remove(vod_id)
            job = pretranscribe_jobs.get(vod_id)
            if job is not None:
                job[0].requestInterruption()
            getattr(self, "_pretranscribe_attempted_ids", set()).discard(vod_id)
            self.status_label.setText("FW 자막추출 취소를 요청했습니다…")
            editor = self._editor_tabs.get(vod_id)
            if editor is not None:
                editor.status_label.setText("FW 자막추출 취소를 요청했습니다…")
            return
        auxiliary_job = self._active_auxiliary_ai_job(vod_id)
        if auxiliary_job is not None:
            target_vod_id, job_name, thread = auxiliary_job
            thread.requestInterruption()
            for editor_vod_id in {vod_id, target_vod_id}:
                editor = self._editor_tabs.get(editor_vod_id)
                if editor is not None:
                    editor.request_auxiliary_ai_cancel(job_name)
            self.status_label.setText(f"{job_name} 취소를 요청했습니다…")

    def open_vod_from_row(self, row: int, column: int) -> None:
        del column
        item = self.vod_table.item(row, 0)
        if item is None:
            return
        self.open_timeline(str(item.data(Qt.ItemDataRole.UserRole)))

    def _close_tab(self, index: int) -> None:
        if index == 0:
            return
        widget = self.tabs.widget(index)
        if isinstance(widget, TimelineDocumentEditor):
            if self._vod_active_job(widget.vod.vod_id):
                message = QMessageBox(self)
                message.setWindowTitle("분석 작업 중")
                message.setIcon(QMessageBox.Icon.Information)
                message.setText(
                    "AI 작업을 취소하거나 완료한 뒤 탭을 닫으세요."
                )
                cancel_button = message.addButton(
                    "작업 취소",
                    QMessageBox.ButtonRole.DestructiveRole,
                )
                message.addButton(
                    "계속 작업",
                    QMessageBox.ButtonRole.RejectRole,
                )
                message.exec()
                if message.clickedButton() is cancel_button:
                    self._cancel_or_dequeue(widget.vod.vod_id)
                return
            widget.flush_memo_save()
            self._save_timeline(widget.vod.vod_id, widget.text())
            self._editor_tabs.pop(widget.vod.vod_id, None)
            widget.close_review_player()
            publisher = self._comment_publishers.pop(widget.vod.vod_id, None)
            if publisher is not None:
                publisher.close()
        self.tabs.removeTab(index)
        widget.deleteLater()

    def _flush_editor_memos(self) -> None:
        for editor in self._editor_tabs.values():
            editor.flush_memo_save()

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(
        self, reason: QSystemTrayIcon.ActivationReason
    ) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._show_from_tray()

    def _quit_from_tray(self) -> None:
        self._force_quit = True
        self.close()

    def _notify_tray_running(self) -> None:
        if self._tray_hint_shown or self.tray_icon is None:
            return
        self._tray_hint_shown = True
        self.tray_icon.showMessage(
            "SOOP AI 타임라인",
            "백그라운드에서 계속 실행 중입니다. 트레이 아이콘을 우클릭해 "
            "'종료'를 눌러야 완전히 종료됩니다.",
            QSystemTrayIcon.MessageIcon.Information,
            5_000,
        )

    def _quit_application(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.tray_icon is not None and not self._force_quit:
            # The close button keeps the app running in the system tray.
            event.ignore()
            self.hide()
            self._notify_tray_running()
            return
        if self._update_download_job is not None:
            self._quit_after_update_cancel = True
            self._update_download_job[0].requestInterruption()
            self.status_label.setText("업데이트 다운로드를 정리한 뒤 종료합니다…")
            event.ignore()
            return
        self._flush_editor_memos()
        if not self._active_jobs():
            self._close_auxiliary_windows()
            event.accept()
            self._quit_application()
            return
        answer = QMessageBox.question(
            self,
            "AI 작업 진행 중",
            "진행 중인 AI 작업을 취소하고 프로그램을 종료할까요?\n"
            "현재 API 요청 또는 분석 구간이 끝날 때까지 잠시 걸릴 수 있습니다.\n\n"
            "진행 중인 라이브는 종료 처리하지 않고 다음 실행 때 같은 방송에 "
            "자동 재연결합니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._force_quit = False
            event.ignore()
            return
        self._close_after_analysis = True
        self._analysis_queue.clear()
        self.database.clear_analysis_queue()
        for thread, _ in self._analysis_jobs.values():
            thread.requestInterruption()
        for thread, _ in self._style_jobs.values():
            thread.requestInterruption()
        for thread, _ in self._line_rewrite_jobs.values():
            thread.requestInterruption()
        for vod_id, (thread, worker) in self._live_jobs.items():
            self._live_shutdown_resume_ids.add(vod_id)
            editor = self._editor_tabs.get(vod_id)
            if editor is not None:
                self.database.save_timeline(
                    vod_id,
                    editor.text(),
                    VodState.ANALYZING.value,
                )
            self.database.set_vod_state(vod_id, VodState.ANALYZING.value)
            worker.request_stop(finalize=False)
            thread.requestInterruption()
        for thread, _ in self._regroup_jobs.values():
            thread.requestInterruption()
        if self._manual_link_job is not None:
            self._manual_link_job[0].requestInterruption()
        if self._live_reconnect_job is not None:
            if self._live_reconnect_target_id:
                self._live_shutdown_resume_ids.add(
                    self._live_reconnect_target_id
                )
            self._live_reconnect_job[0].requestInterruption()
        for thread, _ in self._pretranscribe_jobs.values():
            thread.requestInterruption()
        self._pretranscribe_queue.clear()
        for publisher in self._comment_publishers.values():
            publisher.cancel_publish(update_status=False)
        if self._active_jobs():
            self.status_label.setText("AI 작업 취소 후 프로그램을 종료합니다…")
            event.ignore()
            return
        self._close_auxiliary_windows()
        event.accept()
        self._quit_application()

    def _close_auxiliary_windows(self) -> None:
        for editor in list(self._editor_tabs.values()):
            editor.close_review_player()
        for window in list(self._transcript_windows.values()):
            window.close()
        self._transcript_windows.clear()
        for publisher in list(self._comment_publishers.values()):
            publisher.close()
        self._comment_publishers.clear()

    def _active_jobs(self) -> bool:
        return bool(
            self._analysis_jobs
            or self._analysis_queue
            or self._style_jobs
            or self._line_rewrite_jobs
            or self._live_jobs
            or self._regroup_jobs
            or self._manual_link_job is not None
            or self._live_reconnect_job is not None
            or self._pretranscribe_jobs
            or self._pretranscribe_queue
            or any(publisher.is_busy for publisher in self._comment_publishers.values())
        )
