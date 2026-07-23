from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from urllib.parse import quote

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile

from ..models import Streamer


EXTRACT_VODS_SCRIPT = r"""
JSON.stringify((() => {
  const numericVod = /\/player\/(\d+)(?:[/?#]|$)/;
  const anchors = Array.from(document.querySelectorAll('a[href*="vod.sooplive.com/player/"]'));
  const seen = new Set();
  const items = [];

  for (const anchor of anchors) {
    const href = anchor.href || '';
    const idMatch = href.match(numericVod);
    if (!idMatch || seen.has(idMatch[1])) continue;

    const card = anchor.closest('[class*="VodList_item__"]')
      || anchor.closest('[class*="VodList_itemContainer"]');
    if (!card) continue;

    const lines = (card.innerText || '')
      .split('\n')
      .map(value => value.trim())
      .filter(Boolean);

    // The /vod/review page should already contain replays only. Keep this guard
    // so a future route change does not silently add clips to the work queue.
    if (!lines.includes('다시보기')) continue;

    const titleLink = Array.from(card.querySelectorAll('a'))
      .find(value => value.href === href && (value.innerText || '').trim());
    const title = titleLink ? titleLink.innerText.trim() : '';
    if (!title) continue;

    const duration = lines.find(value => /^\d{1,3}:\d{2}(?::\d{2})?$/.test(value)) || '';
    const published = lines.find(value =>
      /^\d{4}-\d{2}-\d{2}$/.test(value)
      || /^\d+\s*(?:초|분|시간|일)\s*전$/.test(value)
    ) || '';
    const image = card.querySelector('img');

    seen.add(idMatch[1]);
    items.push({
      vod_id: idMatch[1],
      title,
      url: `https://vod.sooplive.com/player/${idMatch[1]}`,
      duration,
      published,
      thumbnail: image ? (image.currentSrc || image.src || '') : ''
    });

    if (items.length >= 1000) break;
  }

  const title = document.title || '';
  const nameMatch = title.match(/^(.*?)의 방송국\s*\|\s*SOOP$/);
  const bodyText = (document.body ? document.body.innerText : '').slice(0, 5000);
  const explicitEmpty = /(?:등록된|업로드된|보관된)\s*(?:다시보기|VOD).{0,20}(?:없습니다|없어요)/i.test(bodyText)
    || /(?:다시보기|VOD).{0,20}(?:등록|업로드).{0,20}(?:없습니다|없어요)/i.test(bodyText);
  const blocked = /Access Denied|Cloudflare|captcha|비정상적인 접근|접근이 제한/i.test(bodyText);
  return {
    streamer_name: nameMatch ? nameMatch[1].trim() : '',
    items,
    page_title: title,
    ready_state: document.readyState,
    explicit_empty: explicitEmpty,
    blocked,
    vod_anchor_count: anchors.length,
    has_vod_container: !!document.querySelector('[class*="VodList_"]')
  };
})())
"""

LOAD_MORE_SCRIPT = r"""
(() => {
  const candidates = Array.from(document.querySelectorAll('button, a'));
  const more = candidates.find(element =>
    /^(?:더\s*보기|더보기|이전\s*영상|이전영상)$/.test(
      (element.innerText || element.textContent || '').trim()
    )
  );
  if (more) {
    try { more.click(); } catch (_) {}
  }
  const root = document.scrollingElement || document.documentElement;
  window.scrollTo({ top: root.scrollHeight, behavior: 'instant' });
  return { clicked: Boolean(more), height: root.scrollHeight };
})()
"""


@dataclass(frozen=True, slots=True)
class DiscoveryDecision:
    action: str
    message: str = ""


def classify_discovery_result(
    result: dict[str, object],
    elapsed_ms: int,
    max_wait_ms: int,
) -> DiscoveryDecision:
    if bool(result.get("blocked")):
        return DiscoveryDecision(
            "error",
            "SOOP 페이지가 자동 확인 요청을 차단했습니다. 잠시 뒤 다시 시도하세요.",
        )
    items = result.get("items")
    if not isinstance(items, list):
        return DiscoveryDecision("error", "VOD 목록을 해석하지 못했습니다.")
    if items:
        return DiscoveryDecision("success")
    if bool(result.get("explicit_empty")):
        return DiscoveryDecision("success")
    if str(result.get("ready_state", "")) != "complete" or elapsed_ms < max_wait_ms:
        return DiscoveryDecision("wait")
    return DiscoveryDecision(
        "error",
        "VOD 목록이 비어 있고 빈 목록 안내도 확인되지 않았습니다. "
        "SOOP 페이지 구조가 바뀌었거나 로딩이 완료되지 않았을 수 있습니다.",
    )


def additional_vod_items(
    items: list[object],
    known_vod_ids: set[str],
    *,
    limit: int = 30,
) -> list[dict[str, object]]:
    additional: list[dict[str, object]] = []
    seen = set(known_vod_ids)
    for item in items:
        if not isinstance(item, dict):
            continue
        vod_id = str(item.get("vod_id", "") or "").strip()
        if not vod_id or vod_id in seen:
            continue
        seen.add(vod_id)
        additional.append(item)
        if len(additional) >= limit:
            break
    return additional


class SoopVodDiscovery(QObject):
    started = Signal(int)
    progress = Signal(str)
    result_ready = Signal(int, str, object)
    streamer_error = Signal(int, str)
    finished = Signal(int, int)

    def __init__(
        self,
        parent: QObject | None = None,
        settle_ms: int = 750,
        max_wait_ms: int = 15_000,
    ):
        super().__init__(parent)
        self._settle_ms = settle_ms
        self._max_wait_ms = max(settle_ms, max_wait_ms)
        self._elapsed_ms = 0
        self._queue: deque[Streamer] = deque()
        self._current: Streamer | None = None
        self._new_count = 0
        self._error_count = 0
        self._busy = False
        self._load_more_mode = False
        self._known_vod_ids: set[str] = set()
        self._additional_items: list[dict[str, object]] = []
        self._additional_ids: set[str] = set()
        self._last_anchor_count = -1
        self._stagnant_rounds = 0

        self._profile = QWebEngineProfile(self)
        self._profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        self._profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
        )
        self._page = QWebEnginePage(self._profile, self)
        self._page.loadFinished.connect(self._on_load_finished)

    @property
    def busy(self) -> bool:
        return self._busy

    def refresh(
        self,
        streamers: list[Streamer],
        *,
        include_disabled: bool = False,
    ) -> None:
        if self._busy:
            return
        enabled = (
            list(streamers)
            if include_disabled
            else [streamer for streamer in streamers if streamer.enabled]
        )
        if not enabled:
            self.finished.emit(0, 0)
            return

        self._queue = deque(enabled)
        self._new_count = 0
        self._error_count = 0
        self._load_more_mode = False
        self._known_vod_ids.clear()
        self._reset_load_more_progress()
        self._busy = True
        self.started.emit(len(enabled))
        self._load_next()

    def load_more(
        self,
        streamer: Streamer,
        known_vod_ids: set[str],
    ) -> None:
        if self._busy:
            return
        self._queue = deque([streamer])
        self._new_count = 0
        self._error_count = 0
        self._load_more_mode = True
        self._known_vod_ids = {str(vod_id) for vod_id in known_vod_ids}
        self._reset_load_more_progress()
        self._busy = True
        self.started.emit(1)
        self._load_next()

    def _reset_load_more_progress(self) -> None:
        self._additional_items = []
        self._additional_ids = set()
        self._last_anchor_count = -1
        self._stagnant_rounds = 0

    def _load_next(self) -> None:
        if not self._queue:
            self._busy = False
            self._current = None
            self.finished.emit(self._new_count, self._error_count)
            return

        self._current = self._queue.popleft()
        if self._load_more_mode:
            self.progress.emit(f"{self._current.display_name} 과거 영상 불러오는 중…")
        else:
            self.progress.emit(f"{self._current.display_name} 신규 영상 확인 중…")
        channel = quote(self._current.channel_id, safe="")
        url = QUrl(f"https://www.sooplive.com/station/{channel}/vod/review")
        self._page.load(url)

    def _on_load_finished(self, ok: bool) -> None:
        if self._current is None:
            return
        if not ok:
            self._fail_current("공개 VOD 페이지를 불러오지 못했습니다.")
            return
        self._elapsed_ms = 0
        QTimer.singleShot(self._settle_ms, self._extract_current)

    def _extract_current(self) -> None:
        if self._current is None:
            return
        self._page.runJavaScript(EXTRACT_VODS_SCRIPT, self._on_extracted)

    def _on_extracted(self, result: object) -> None:
        current = self._current
        if current is None:
            return
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                self._fail_current("VOD 목록 응답을 해석하지 못했습니다.")
                return
        if not isinstance(result, dict):
            self._fail_current("VOD 목록 응답 형식이 변경되었습니다.")
            return

        if self._load_more_mode:
            self._on_load_more_extracted(current, result)
            return

        self._elapsed_ms += self._settle_ms
        decision = classify_discovery_result(
            result,
            self._elapsed_ms,
            self._max_wait_ms,
        )
        if decision.action == "wait":
            current_name = current.display_name
            self.progress.emit(
                f"{current_name} VOD 목록 로딩 대기 중… "
                f"{self._elapsed_ms / 1000:.0f}초"
            )
            QTimer.singleShot(self._settle_ms, self._extract_current)
            return
        if decision.action == "error":
            self._fail_current(decision.message)
            return

        items = result.get("items", [])
        if isinstance(items, list):
            items = items[:30]

        # Qt may return nested wrappers on some versions; JSON round-tripping
        # produces plain Python data for the database boundary.
        clean_items = json.loads(json.dumps(items, ensure_ascii=False))
        streamer_name = str(result.get("streamer_name", "") or "")
        self.result_ready.emit(current.id, streamer_name, clean_items)
        self._new_count += len(clean_items)
        self._current = None
        QTimer.singleShot(200, self._load_next)

    def _on_load_more_extracted(
        self,
        current: Streamer,
        result: dict[str, object],
    ) -> None:
        if bool(result.get("blocked")):
            self._fail_current(
                "SOOP 페이지가 과거 영상 요청을 차단했습니다. 잠시 뒤 다시 시도하세요."
            )
            return
        items = result.get("items")
        if not isinstance(items, list):
            self._fail_current("VOD 목록을 해석하지 못했습니다.")
            return

        clean_items = json.loads(json.dumps(items, ensure_ascii=False))
        unseen = additional_vod_items(
            clean_items,
            self._known_vod_ids | self._additional_ids,
            limit=30 - len(self._additional_items),
        )
        self._additional_items.extend(unseen)
        self._additional_ids.update(str(item["vod_id"]) for item in unseen)
        if len(self._additional_items) >= 30:
            self._finish_load_more_current(current)
            return

        anchor_count = int(result.get("vod_anchor_count", len(items)) or len(items))
        if unseen or anchor_count > self._last_anchor_count:
            self._elapsed_ms = 0
            self._stagnant_rounds = 0
        else:
            self._elapsed_ms += self._settle_ms
            self._stagnant_rounds += 1
        self._last_anchor_count = max(self._last_anchor_count, anchor_count)

        if self._elapsed_ms >= self._max_wait_ms or self._stagnant_rounds >= 5:
            self._finish_load_more_current(current)
            return

        self.progress.emit(
            f"{current.display_name} 과거 영상 탐색 중… "
            f"{len(self._additional_items):,}/30개"
        )
        self._page.runJavaScript(LOAD_MORE_SCRIPT)
        QTimer.singleShot(self._settle_ms, self._extract_current)

    def _finish_load_more_current(self, current: Streamer) -> None:
        streamer_name = current.display_name
        self.result_ready.emit(
            current.id,
            streamer_name,
            self._additional_items[:30],
        )
        self._new_count += len(self._additional_items[:30])
        self._current = None
        self._reset_load_more_progress()
        QTimer.singleShot(200, self._load_next)

    def _fail_current(self, message: str) -> None:
        current = self._current
        if current is not None:
            self._error_count += 1
            self.streamer_error.emit(current.id, message)
        self._current = None
        QTimer.singleShot(200, self._load_next)
