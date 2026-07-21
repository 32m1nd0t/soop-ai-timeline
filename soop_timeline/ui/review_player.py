from __future__ import annotations

from PySide6.QtCore import QTimer, QUrl, QUrlQuery, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from qtwebview2 import QtWebView2Widget

from ..models import Vod
from ..paths import app_data_dir
from ..services.timeline_timestamp import format_timestamp_seconds


def build_player_url(vod_id: str) -> QUrl:
    url = QUrl(f"https://vod.sooplive.com/player/{vod_id}/embed")
    query = QUrlQuery()
    query.addQueryItem("autoPlay", "true")
    query.addQueryItem("mutePlay", "true")
    query.addQueryItem("showChat", "false")
    url.setQuery(query)
    return url


def build_seek_script(seconds: int) -> str:
    target = max(0, int(seconds))
    return f"""
const target = {target};

// When the streamer is currently live, SOOP can place a 'VOD 보기' layer
// over the requested replay. Select the replay before looking for its video.
const vodButton = Array.from(
    document.querySelectorAll('button, a, [role="button"]')
).find((element) => (element.textContent || '').trim() === 'VOD 보기');
if (vodButton && vodButton.offsetParent !== null) {{
    vodButton.click();
}}

const video = document.querySelector('video#video')
    || document.querySelector('#video video')
    || document.querySelector('video');
if (!video) {{
    return {{ ok: false, reason: 'video-not-found' }};
}}

const duration = Number(video.duration);
let seekableEnd = 0;
try {{
    if (video.seekable && video.seekable.length) {{
        seekableEnd = Number(video.seekable.end(video.seekable.length - 1));
    }}
}} catch (_) {{}}

if ((!Number.isFinite(duration) || duration <= 0) && seekableEnd <= 0) {{
    return {{ ok: false, reason: 'metadata-not-ready' }};
}}

const availableEnd = Number.isFinite(duration) && duration > 0
    ? duration
    : seekableEnd;
if (availableEnd > 0 && target > availableEnd + 2) {{
    return {{
        ok: false,
        reason: 'target-outside-media',
        duration: availableEnd
    }};
}}

try {{
    video.currentTime = target;
    video.muted = false;
    try {{ await video.play(); }} catch (_) {{}}
    await new Promise((resolve) => setTimeout(resolve, 250));
    return {{
        ok: true,
        currentTime: Number(video.currentTime),
        duration: availableEnd,
        paused: Boolean(video.paused),
        muted: Boolean(video.muted)
    }};
}} catch (error) {{
    return {{
        ok: false,
        reason: 'seek-failed',
        message: String(error)
    }};
}}
""".strip()


def build_player_action_script(action: str, value: int = 0) -> str:
    safe_action = action if action in {"position", "toggle", "relative"} else "position"
    amount = int(value)
    return f"""
const video = document.querySelector('video#video')
    || document.querySelector('#video video')
    || document.querySelector('video');
if (!video) {{
    return {{ ok: false, reason: 'video-not-found' }};
}}
const action = {safe_action!r};
if (action === 'toggle') {{
    if (video.paused) {{ try {{ await video.play(); }} catch (_) {{}} }}
    else {{ video.pause(); }}
}} else if (action === 'relative') {{
    const duration = Number(video.duration);
    const target = Math.max(0, Number(video.currentTime || 0) + {amount});
    video.currentTime = Number.isFinite(duration) && duration > 0
        ? Math.min(target, duration)
        : target;
}}
return {{
    ok: true,
    currentTime: Number(video.currentTime || 0),
    paused: Boolean(video.paused)
}};
""".strip()


class SoopReviewPlayer(QFrame):
    closed = Signal()
    seek_completed = Signal(int)
    status_changed = Signal(str)
    current_time_ready = Signal(int)

    def __init__(self, vod: Vod, parent: QWidget | None = None):
        super().__init__(parent)
        self.vod = vod
        self.setObjectName("playerCard")
        self.setMinimumWidth(380)

        self._loaded = False
        self._dom_loaded = False
        self._pending_seconds: int | None = None
        self._seek_generation = 0
        self._seek_attempts = 0
        self._seek_in_flight = False

        self._retry_timer = QTimer(self)
        self._retry_timer.setInterval(500)
        self._retry_timer.timeout.connect(self._attempt_seek)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("SOOP 검수 플레이어")
        title.setObjectName("sectionTitle")
        reload_button = QPushButton("새로고침")
        reload_button.clicked.connect(self.reload)
        external_button = QPushButton("외부로 열기")
        external_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(self.vod.url))
        )
        close_button = QPushButton("닫기")
        close_button.clicked.connect(self.close_player)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(reload_button)
        header.addWidget(external_button)
        header.addWidget(close_button)
        layout.addLayout(header)

        self.time_label = QLabel("시간을 더블클릭하세요")
        self.time_label.setObjectName("muted")
        layout.addWidget(self.time_label)

        browser_data = app_data_dir() / "webview2"
        browser_data.mkdir(parents=True, exist_ok=True)
        self.web_view = QtWebView2Widget(
            debug=False,
            context_menus=False,
            background_color="#000000",
            handle_new_window=True,
            lazyload=True,
            user_data_folder=str(browser_data),
            fullscreen_support=True,
            parent=self,
        )
        self.web_view.setMinimumHeight(260)
        self.web_view.bridge.initialization_done.connect(
            self._on_webview_initialized
        )
        self.web_view.bridge.domContentLoaded.connect(self._on_dom_loaded)
        layout.addWidget(self.web_view, 1)

        self.help_label = QLabel(
            "타임라인의 시간을 더블클릭하면 이 플레이어가 같은 지점으로 이동합니다. "
            "광고가 표시되면 광고가 끝난 뒤 시간을 한 번 더 더블클릭하세요."
        )
        self.help_label.setObjectName("muted")
        self.help_label.setWordWrap(True)
        layout.addWidget(self.help_label)

    def open_player(self) -> None:
        self.setVisible(True)
        if not self._loaded:
            self._loaded = True
            self.status_changed.emit("SOOP 검수 플레이어를 불러옵니다…")
            self.time_label.setText("Edge 플레이어 준비 중…")
            self.web_view.load_url(build_player_url(self.vod.vod_id).toString())

    def seek_to(self, seconds: int) -> None:
        value = max(0, int(seconds))
        self.open_player()
        self._pending_seconds = value
        self._seek_generation += 1
        self._seek_attempts = 0
        label = format_timestamp_seconds(value)
        self.time_label.setText(f"이동 중 · {label}")
        self.status_changed.emit(f"SOOP 영상을 {label} 지점으로 이동합니다…")
        if self._dom_loaded:
            self._attempt_seek()
        if not self._retry_timer.isActive():
            self._retry_timer.start()

    def reload(self) -> None:
        self.open_player()
        self._dom_loaded = False
        self.time_label.setText("새로고침 중…")
        self.web_view.reload()

    def request_current_time(self) -> None:
        self.open_player()
        if not self._dom_loaded or not self.web_view.is_ready:
            self.status_changed.emit("플레이어가 준비된 뒤 현재 위치를 다시 눌러 주세요.")
            return
        self.web_view.evaluate_js(
            build_player_action_script("position"),
            lambda result: self._handle_player_action("position", result),
        )

    def toggle_playback(self) -> None:
        self.open_player()
        if not self._dom_loaded or not self.web_view.is_ready:
            return
        self.web_view.evaluate_js(
            build_player_action_script("toggle"),
            lambda result: self._handle_player_action("toggle", result),
        )

    def seek_relative(self, seconds: int) -> None:
        self.open_player()
        if not self._dom_loaded or not self.web_view.is_ready:
            return
        self.web_view.evaluate_js(
            build_player_action_script("relative", seconds),
            lambda result: self._handle_player_action("relative", result),
        )

    def close_player(self) -> None:
        self._retry_timer.stop()
        self._seek_in_flight = False
        if self.web_view.is_ready:
            self.web_view.evaluate_js(
                "const video=document.querySelector('video'); "
                "if(video){video.pause();} return true;"
            )
        self.setVisible(False)
        self.closed.emit()

    def _on_webview_initialized(self, success: bool, error_message: str) -> None:
        if not success:
            self._retry_timer.stop()
            self.time_label.setText("Edge 플레이어 실행 실패")
            self.status_changed.emit(
                f"Edge WebView2 플레이어를 실행하지 못했습니다. {error_message}".strip()
            )
            return
        self.time_label.setText("SOOP 영상 불러오는 중…")

    def _on_dom_loaded(self) -> None:
        self._dom_loaded = True
        if self._pending_seconds is None:
            self.time_label.setText("시간을 더블클릭하세요")
            self.status_changed.emit("SOOP 검수 플레이어를 열었습니다.")
            return
        self._attempt_seek()
        if not self._retry_timer.isActive():
            self._retry_timer.start()

    def _attempt_seek(self) -> None:
        if (
            not self._dom_loaded
            or not self.web_view.is_ready
            or self._pending_seconds is None
            or self._seek_in_flight
        ):
            return
        if self._seek_attempts >= 60:
            self._retry_timer.stop()
            label = format_timestamp_seconds(self._pending_seconds)
            self.time_label.setText(f"이동 대기 · {label}")
            self.status_changed.emit(
                "플레이어가 아직 준비되지 않았습니다. 광고나 안내 화면을 확인한 뒤 "
                "타임스탬프를 다시 더블클릭하세요."
            )
            return

        self._seek_attempts += 1
        self._seek_in_flight = True
        generation = self._seek_generation
        seconds = self._pending_seconds
        self.web_view.evaluate_js(
            build_seek_script(seconds),
            lambda result: self._handle_seek_result(generation, seconds, result),
        )

    def _handle_seek_result(
        self,
        generation: int,
        seconds: int,
        result: object,
    ) -> None:
        self._seek_in_flight = False
        if generation != self._seek_generation or seconds != self._pending_seconds:
            return
        if not isinstance(result, dict) or not bool(result.get("success")):
            return
        payload = result.get("result")
        if not isinstance(payload, dict) or not bool(payload.get("ok")):
            if isinstance(payload, dict) and payload.get("reason") == "target-outside-media":
                self._retry_timer.stop()
                self._pending_seconds = None
                self.time_label.setText("영상 범위를 벗어난 시간")
                self.status_changed.emit(
                    "선택한 타임스탬프가 실제 영상 길이를 벗어났습니다."
                )
            return

        self._pending_seconds = None
        self._retry_timer.stop()
        label = format_timestamp_seconds(seconds)
        self.time_label.setText(f"재생 위치 · {label}")
        self.status_changed.emit(f"SOOP 영상을 {label} 지점으로 이동했습니다.")
        self.seek_completed.emit(seconds)

    def _handle_player_action(self, action: str, result: object) -> None:
        if not isinstance(result, dict) or not bool(result.get("success")):
            return
        payload = result.get("result")
        if not isinstance(payload, dict) or not bool(payload.get("ok")):
            self.status_changed.emit("검수 플레이어의 재생 위치를 읽지 못했습니다.")
            return
        seconds = max(0, int(float(payload.get("currentTime", 0) or 0)))
        label = format_timestamp_seconds(seconds)
        paused = bool(payload.get("paused"))
        self.time_label.setText(
            f"재생 위치 · {label}" + (" · 일시정지" if paused else "")
        )
        if action == "position":
            self.current_time_ready.emit(seconds)
        elif action == "toggle":
            self.status_changed.emit(
                f"{label} · {'일시정지' if paused else '재생 중'}"
            )
        else:
            self.status_changed.emit(f"검수 영상을 {label} 지점으로 이동했습니다.")
