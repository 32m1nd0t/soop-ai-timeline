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
from ..services.timeline_validation import parse_duration_text


def build_player_url(vod_id: str) -> QUrl:
    url = QUrl(f"https://vod.sooplive.com/player/{vod_id}/embed")
    query = QUrlQuery()
    query.addQueryItem("autoPlay", "true")
    query.addQueryItem("mutePlay", "true")
    query.addQueryItem("showChat", "false")
    url.setQuery(query)
    return url


# SOOP's embed can hold several <video> elements: an empty placeholder plus the
# real replay, and — when the streamer is live again — a 'VOD 보기' promo layer
# over an unloaded (black) replay. This helper dismisses that layer and returns
# the element that actually has media, instead of the first <video> in the DOM.
_PICK_VIDEO_FN = """
function __pickVod() {
    const vodButton = Array.from(
        document.querySelectorAll('button, a, [role="button"]')
    ).find((element) => (element.textContent || '').trim() === 'VOD 보기');
    if (vodButton && vodButton.offsetParent !== null) {
        vodButton.click();
    }
    const videos = Array.from(document.querySelectorAll('video'));
    if (!videos.length) { return null; }
    let best = null;
    let bestScore = -1;
    for (const candidate of videos) {
        const duration = Number(candidate.duration);
        let seekEnd = 0;
        try {
            if (candidate.seekable && candidate.seekable.length) {
                seekEnd = Number(candidate.seekable.end(candidate.seekable.length - 1));
            }
        } catch (_) {}
        let score = 0;
        if (Number.isFinite(duration) && duration > 0) { score += 4; }
        if (seekEnd > 0) { score += 2; }
        if (candidate.currentSrc || candidate.src) { score += 1; }
        score += (Number(candidate.readyState) || 0) * 0.1;
        if (score > bestScore) { bestScore = score; best = candidate; }
    }
    return best;
}
"""


_SOOP_GLOBAL_TIME_FN = """
function __clockSeconds(value) {
    const parts = String(value || '').trim().split(':');
    if (parts.length !== 2 && parts.length !== 3) { return null; }
    const numbers = parts.map((part) => Number(part));
    if (numbers.some((part) => !Number.isFinite(part) || part < 0)) {
        return null;
    }
    if (parts.length === 2) {
        return numbers[0] * 60 + numbers[1];
    }
    return numbers[0] * 3600 + numbers[1] * 60 + numbers[2];
}

function __readSoopClocks() {
    const currentElement = document.querySelector(
        '.time-current, [aria-label="현재 재생 시간"]'
    );
    const durationElement = document.querySelector(
        '.time-duration, [aria-label="전체 재생 시간"]'
    );
    return {
        current: __clockSeconds(currentElement && currentElement.textContent),
        total: __clockSeconds(durationElement && durationElement.textContent)
    };
}

function __dispatchSoopSeek(target, total) {
    const progress = document.querySelector('#player .progress')
        || document.querySelector('.progress')
        || document.querySelector('.progress_track');
    if (!progress || !Number.isFinite(total) || total <= 0) {
        return { ok: false, reason: 'progress-not-ready' };
    }
    const rect = progress.getBoundingClientRect();
    if (!Number.isFinite(rect.width) || rect.width <= 0) {
        return { ok: false, reason: 'progress-not-ready' };
    }
    const ratio = Math.max(0, Math.min(1, target / total));
    const clientX = rect.left + rect.width * ratio;
    const clientY = rect.top + rect.height / 2;
    const base = {
        bubbles: true,
        cancelable: true,
        composed: true,
        view: window,
        clientX,
        clientY,
        screenX: clientX,
        screenY: clientY,
        button: 0
    };
    const emit = (type, buttons) => {
        try {
            const options = { ...base, buttons };
            const event = type.startsWith('pointer')
                && typeof PointerEvent === 'function'
                ? new PointerEvent(type, {
                    ...options,
                    pointerId: 1,
                    pointerType: 'mouse',
                    isPrimary: true
                })
                : new MouseEvent(type, options);
            progress.dispatchEvent(event);
        } catch (_) {}
    };
    for (const type of [
        'pointerover', 'mouseover', 'pointermove', 'mousemove',
        'pointerdown', 'mousedown'
    ]) {
        emit(type, 1);
    }
    for (const type of ['pointerup', 'mouseup', 'click']) {
        emit(type, 0);
    }
    return { ok: true, ratio };
}
"""


def build_seek_script(seconds: int, expected_duration: int | None = None) -> str:
    target = max(0, int(seconds))
    expected_total = max(0, int(expected_duration or 0))
    return (_PICK_VIDEO_FN + _SOOP_GLOBAL_TIME_FN + f"""
const target = {target};
const expectedTotal = {expected_total};
const video = __pickVod();
if (!video) {{
    return {{ ok: false, reason: 'video-not-found' }};
}}

const clocksBefore = __readSoopClocks();
if (expectedTotal > 0) {{
    const durationTolerance = Math.max(30, expectedTotal * 0.01);
    if (!Number.isFinite(clocksBefore.total)
        || Math.abs(clocksBefore.total - expectedTotal) > durationTolerance) {{
        return {{
            ok: false,
            reason: 'player-not-ready',
            displayedDuration: clocksBefore.total
        }};
    }}
}}

const globalTotal = Number.isFinite(clocksBefore.total) && clocksBefore.total > 0
    ? clocksBefore.total
    : expectedTotal;

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

// When the first media part is already active, seek it directly. Using the
// full six-hour progress bar here is too coarse (roughly 20 seconds per pixel)
// and repeated verification attempts can make a short section loop forever.
const firstPartIsActive = !Number.isFinite(clocksBefore.current)
    || clocksBefore.current < availableEnd + 5;
if (firstPartIsActive && target < availableEnd - 1) {{
    try {{
        video.currentTime = target;
        video.muted = false;
        try {{ await video.play(); }} catch (_) {{}}
        await new Promise((resolve) => setTimeout(resolve, 350));
        const landed = Number(video.currentTime);
        if (Number.isFinite(landed) && Math.abs(landed - target) <= 5) {{
            return {{
                ok: true,
                strategy: 'active-part-current-time',
                currentTime: landed,
                duration: globalTotal || availableEnd,
                paused: Boolean(video.paused),
                muted: Boolean(video.muted)
            }};
        }}
        return {{
            ok: false,
            reason: 'target-not-ready',
            issued: true,
            landed,
            duration: availableEnd,
            strategy: 'active-part-current-time'
        }};
    }} catch (error) {{
        return {{
            ok: false,
            reason: 'seek-failed',
            message: String(error)
        }};
    }}
}}

if (globalTotal > 0) {{
    const dispatched = __dispatchSoopSeek(target, globalTotal);
    if (dispatched.ok) {{
        await new Promise((resolve) => setTimeout(resolve, 900));
        const activeVideo = __pickVod() || video;
        try {{
            activeVideo.muted = false;
            try {{ await activeVideo.play(); }} catch (_) {{}}
        }} catch (_) {{}}
        let clocksAfter = __readSoopClocks();
        let landedGlobal = clocksAfter.current;
        // The full progress bar is only about one pixel per 20 seconds on a
        // six-hour replay. Once SOOP has switched to the correct part, correct
        // that pixel rounding against the active part's local currentTime.
        if (Number.isFinite(landedGlobal)
            && Math.abs(landedGlobal - target) <= 30) {{
            const localCurrent = Number(activeVideo.currentTime);
            const localDuration = Number(activeVideo.duration);
            const correction = target - landedGlobal;
            const correctedLocal = localCurrent + correction;
            if (Math.abs(correction) > 1
                && Number.isFinite(localCurrent)
                && Number.isFinite(localDuration)
                && correctedLocal >= 0
                && correctedLocal <= localDuration) {{
                try {{
                    activeVideo.currentTime = correctedLocal;
                    await new Promise((resolve) => setTimeout(resolve, 300));
                    clocksAfter = __readSoopClocks();
                    landedGlobal = clocksAfter.current;
                }} catch (_) {{}}
            }}
        }}
        if (Number.isFinite(landedGlobal)
            && Math.abs(landedGlobal - target) <= 5) {{
            return {{
                ok: true,
                strategy: 'soop-progress',
                currentTime: landedGlobal,
                duration: globalTotal,
                paused: Boolean(activeVideo.paused),
                muted: Boolean(activeVideo.muted)
            }};
        }}
        return {{
            ok: false,
            reason: 'target-not-ready',
            issued: true,
            landed: landedGlobal,
            duration: globalTotal,
            strategy: 'soop-progress'
        }};
    }}
}}

// A multipart replay exposes only the active part through <video>. Never use a
// global timestamp as that part's local currentTime; wait for SOOP's own
// full-duration progress control instead.
if (globalTotal > availableEnd + 15) {{
    return {{
        ok: false,
        reason: 'progress-not-ready',
        duration: availableEnd,
        globalDuration: globalTotal
    }};
}}

try {{
    video.currentTime = target;
    video.muted = false;
    try {{ await video.play(); }} catch (_) {{}}
    await new Promise((resolve) => setTimeout(resolve, 350));
    const landed = Number(video.currentTime);
    if (Number.isFinite(landed) && Math.abs(landed - target) > 5) {{
        return {{
            ok: false,
            reason: 'target-not-ready',
            issued: true,
            landed: landed,
            duration: availableEnd,
            strategy: 'video-current-time'
        }};
    }}
    return {{
        ok: true,
        strategy: 'video-current-time',
        currentTime: landed,
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
""").strip()


def build_seek_verification_script(seconds: int) -> str:
    """Read the SOOP global clock without issuing another seek command."""
    target = max(0, int(seconds))
    return (_SOOP_GLOBAL_TIME_FN + f"""
const target = {target};
const clocks = __readSoopClocks();
if (!Number.isFinite(clocks.current)) {{
    return {{ ok: false, reason: 'seek-still-pending' }};
}}
if (Math.abs(clocks.current - target) <= 5) {{
    return {{
        ok: true,
        strategy: 'clock-verification',
        currentTime: clocks.current,
        duration: clocks.total
    }};
}}
return {{
    ok: false,
    reason: 'seek-still-pending',
    currentTime: clocks.current,
    duration: clocks.total
}};
""").strip()


def build_activate_script() -> str:
    """Dismiss the live overlay and start muted playback so the replay shows."""
    return (_PICK_VIDEO_FN + """
const video = __pickVod();
if (!video) {
    return { ok: false, reason: 'video-not-found' };
}
try {
    video.muted = true;
    try { await video.play(); } catch (_) {}
} catch (_) {}
return {
    ok: true,
    readyState: Number(video.readyState || 0),
    hasMedia: Boolean(video.currentSrc || video.src)
};
""").strip()


def build_close_script() -> str:
    """Stop every media element so hidden SOOP players cannot keep playing."""
    return """
const videos = Array.from(document.querySelectorAll('video'));
for (const video of videos) {
    try {
        video.pause();
        video.muted = true;
    } catch (_) {}
}
return { ok: true, count: videos.length };
""".strip()


def build_player_action_script(
    action: str,
    value: int = 0,
    expected_duration: int | None = None,
) -> str:
    safe_action = action if action in {"position", "toggle", "relative"} else "position"
    amount = int(value)
    expected_total = max(0, int(expected_duration or 0))
    return (_PICK_VIDEO_FN + _SOOP_GLOBAL_TIME_FN + f"""
let video = __pickVod();
if (!video) {{
    return {{ ok: false, reason: 'video-not-found' }};
}}
const action = {safe_action!r};
const expectedTotal = {expected_total};
let clocks = __readSoopClocks();
if (action !== 'toggle' && expectedTotal > 0) {{
    const durationTolerance = Math.max(30, expectedTotal * 0.01);
    if (!Number.isFinite(clocks.total)
        || Math.abs(clocks.total - expectedTotal) > durationTolerance) {{
        return {{ ok: false, reason: 'player-not-ready' }};
    }}
}}
let reportedCurrent = Number.isFinite(clocks.current)
    ? clocks.current
    : Number(video.currentTime || 0);
if (action === 'toggle') {{
    if (video.paused) {{ try {{ await video.play(); }} catch (_) {{}} }}
    else {{ video.pause(); }}
}} else if (action === 'relative') {{
    const globalTotal = Number.isFinite(clocks.total) && clocks.total > 0
        ? clocks.total
        : expectedTotal;
    if (globalTotal > 0) {{
        const wasPaused = Boolean(video.paused);
        const target = Math.max(0, Math.min(globalTotal, reportedCurrent + {amount}));
        const localCurrent = Number(video.currentTime);
        const localDuration = Number(video.duration);
        const localTarget = localCurrent + {amount};
        if (Number.isFinite(localCurrent)
            && Number.isFinite(localDuration)
            && localTarget >= 0
            && localTarget <= localDuration) {{
            video.currentTime = localTarget;
            await new Promise((resolve) => setTimeout(resolve, 150));
            clocks = __readSoopClocks();
            reportedCurrent = Number.isFinite(clocks.current)
                ? clocks.current
                : target;
        }} else {{
            const dispatched = __dispatchSoopSeek(target, globalTotal);
            if (!dispatched.ok) {{ return dispatched; }}
            await new Promise((resolve) => setTimeout(resolve, 500));
            video = __pickVod() || video;
            if (!wasPaused) {{ try {{ await video.play(); }} catch (_) {{}} }}
            clocks = __readSoopClocks();
            reportedCurrent = Number.isFinite(clocks.current)
                ? clocks.current
                : target;
        }}
    }} else {{
        const duration = Number(video.duration);
        const target = Math.max(0, Number(video.currentTime || 0) + {amount});
        video.currentTime = Number.isFinite(duration) && duration > 0
            ? Math.min(target, duration)
            : target;
        reportedCurrent = Number(video.currentTime || 0);
    }}
}}
if (action === 'toggle') {{
    clocks = __readSoopClocks();
    reportedCurrent = Number.isFinite(clocks.current)
        ? clocks.current
        : Number(video.currentTime || 0);
}}
return {{
    ok: true,
    currentTime: reportedCurrent,
    paused: Boolean(video.paused)
}};
""").strip()


class SoopReviewPlayer(QFrame):
    closed = Signal()
    seek_completed = Signal(int)
    status_changed = Signal(str)
    current_time_ready = Signal(int)

    def __init__(self, vod: Vod, parent: QWidget | None = None):
        super().__init__(parent)
        self.vod = vod
        self._duration_seconds = parse_duration_text(vod.duration_text) or 0
        self.setObjectName("playerCard")
        self.setMinimumWidth(380)

        self._loaded = False
        self._dom_loaded = False
        self._pending_seconds: int | None = None
        self._seek_generation = 0
        self._seek_attempts = 0
        self._seek_in_flight = False
        self._activate_attempts = 0
        self._activate_generation = 0
        self._suppress_activate = False
        self._seek_command_sent = False

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
        elif self._dom_loaded and self._pending_seconds is None:
            self._activate_playback()

    def _activate_playback(self) -> None:
        """Nudge the replay to load and play so the player is not left black."""
        self._activate_generation += 1
        generation = self._activate_generation
        self._activate_attempts = 0
        self._activate_once(generation)

    def _activate_once(self, generation: int) -> None:
        if generation != self._activate_generation or not self.isVisible():
            return
        if self._suppress_activate or self._pending_seconds is not None:
            return  # Never re-mute after the user has sought to a position.
        if not self._dom_loaded or not self.web_view.is_ready:
            return
        self.web_view.evaluate_js(build_activate_script())
        self._activate_attempts += 1
        # The replay video only attaches its source after the live overlay is
        # dismissed, so retry a few times over the first few seconds.
        if self._activate_attempts < 4:
            QTimer.singleShot(
                1200,
                lambda expected=generation: self._activate_once(expected),
            )

    def seek_to(self, seconds: int) -> None:
        value = max(0, int(seconds))
        # Once the user seeks they want audio; never let the muted auto-activate
        # loop run again and re-mute the video.
        self._suppress_activate = True
        self.open_player()
        self._pending_seconds = value
        self._seek_generation += 1
        self._seek_attempts = 0
        self._seek_command_sent = False
        label = format_timestamp_seconds(value)
        self.time_label.setText(f"이동 중 · {label}")
        self.status_changed.emit(f"SOOP 영상을 {label} 지점으로 이동합니다…")
        if self._dom_loaded:
            self._attempt_seek()
        if not self._retry_timer.isActive():
            self._retry_timer.start()

    def reload(self) -> None:
        self._activate_generation += 1
        self.open_player()
        self._dom_loaded = False
        self._suppress_activate = False
        self._seek_command_sent = False
        self._seek_attempts = 0
        self.time_label.setText("새로고침 중…")
        self.web_view.reload()

    def request_current_time(self) -> None:
        self.open_player()
        if not self._dom_loaded or not self.web_view.is_ready:
            self.status_changed.emit("플레이어가 준비된 뒤 현재 위치를 다시 눌러 주세요.")
            return
        self.web_view.evaluate_js(
            build_player_action_script(
                "position",
                expected_duration=self._duration_seconds,
            ),
            lambda result: self._handle_player_action("position", result),
        )

    def toggle_playback(self) -> None:
        self.open_player()
        if not self._dom_loaded or not self.web_view.is_ready:
            return
        self.web_view.evaluate_js(
            build_player_action_script(
                "toggle",
                expected_duration=self._duration_seconds,
            ),
            lambda result: self._handle_player_action("toggle", result),
        )

    def seek_relative(self, seconds: int) -> None:
        self.open_player()
        if not self._dom_loaded or not self.web_view.is_ready:
            return
        self.web_view.evaluate_js(
            build_player_action_script(
                "relative",
                seconds,
                expected_duration=self._duration_seconds,
            ),
            lambda result: self._handle_player_action("relative", result),
        )

    def close_player(self) -> None:
        # Invalidate delayed auto-activation callbacks before hiding the widget;
        # otherwise one can call play() again after this method has paused it.
        self._activate_generation += 1
        self._retry_timer.stop()
        self._pending_seconds = None
        self._seek_generation += 1
        self._seek_attempts = 0
        self._seek_in_flight = False
        self._seek_command_sent = False
        self._suppress_activate = False
        if self.web_view.is_ready:
            self.web_view.evaluate_js(build_close_script())
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
            self._activate_playback()
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
        if self._seek_attempts >= 180:
            self._retry_timer.stop()
            label = format_timestamp_seconds(self._pending_seconds)
            self._pending_seconds = None
            self.time_label.setText(f"이동 대기 · {label}")
            if self._seek_command_sent:
                self.status_changed.emit(
                    "이동 명령은 한 번만 보냈지만 플레이어의 도착 시각을 확인하지 "
                    "못했습니다. 재생 화면을 확인해 주세요."
                )
            else:
                self.status_changed.emit(
                    "플레이어가 아직 준비되지 않았습니다. 광고가 끝났는지 확인한 뒤 "
                    "타임스탬프를 다시 더블클릭하세요."
                )
            self._seek_command_sent = False
            return

        self._seek_attempts += 1
        self._seek_in_flight = True
        generation = self._seek_generation
        seconds = self._pending_seconds
        script = (
            build_seek_verification_script(seconds)
            if self._seek_command_sent
            else build_seek_script(seconds, self._duration_seconds)
        )
        self.web_view.evaluate_js(
            script,
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
            reason = payload.get("reason") if isinstance(payload, dict) else None
            issued = bool(payload.get("issued")) if isinstance(payload, dict) else False
            if reason == "target-not-ready" and issued:
                # The actual seek was already issued. From now on the timer
                # only reads SOOP's global clock and never sends the same seek
                # again, preventing a one-to-two-second playback loop.
                self._seek_command_sent = True
                self._seek_attempts = 0
                label = format_timestamp_seconds(seconds)
                self.time_label.setText(f"이동 후 로딩 중 · {label}")
                self.status_changed.emit(
                    "이동 명령을 한 번 보냈습니다. 재생 위치를 확인하고 있습니다…"
                )
            elif reason == "seek-still-pending":
                if self._seek_attempts == 8:
                    label = format_timestamp_seconds(seconds)
                    self.time_label.setText(f"재생 위치 확인 중 · {label}")
            elif reason == "player-not-ready" and self._seek_attempts == 4:
                self.time_label.setText("광고·본편 전환 대기 중…")
            return

        self._pending_seconds = None
        self._retry_timer.stop()
        self._seek_command_sent = False
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
