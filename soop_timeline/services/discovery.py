from __future__ import annotations

import json
from collections import deque
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

    if (items.length >= 30) break;
  }

  const title = document.title || '';
  const nameMatch = title.match(/^(.*?)의 방송국\s*\|\s*SOOP$/);
  return {
    streamer_name: nameMatch ? nameMatch[1].trim() : '',
    items,
    page_title: title
  };
})())
"""


class SoopVodDiscovery(QObject):
    started = Signal(int)
    progress = Signal(str)
    result_ready = Signal(int, str, object)
    streamer_error = Signal(int, str)
    finished = Signal(int, int)

    def __init__(self, parent: QObject | None = None, settle_ms: int = 1_800):
        super().__init__(parent)
        self._settle_ms = settle_ms
        self._queue: deque[Streamer] = deque()
        self._current: Streamer | None = None
        self._new_count = 0
        self._error_count = 0
        self._busy = False

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

    def refresh(self, streamers: list[Streamer]) -> None:
        if self._busy:
            return
        enabled = [streamer for streamer in streamers if streamer.enabled]
        if not enabled:
            self.finished.emit(0, 0)
            return

        self._queue = deque(enabled)
        self._new_count = 0
        self._error_count = 0
        self._busy = True
        self.started.emit(len(enabled))
        self._load_next()

    def _load_next(self) -> None:
        if not self._queue:
            self._busy = False
            self._current = None
            self.finished.emit(self._new_count, self._error_count)
            return

        self._current = self._queue.popleft()
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

        items = result.get("items", [])
        if not isinstance(items, list):
            self._fail_current("VOD 목록을 해석하지 못했습니다.")
            return

        # Qt may return nested wrappers on some versions; JSON round-tripping
        # produces plain Python data for the database boundary.
        clean_items = json.loads(json.dumps(items, ensure_ascii=False))
        streamer_name = str(result.get("streamer_name", "") or "")
        self.result_ready.emit(current.id, streamer_name, clean_items)
        self._new_count += len(clean_items)
        self._current = None
        QTimer.singleShot(200, self._load_next)

    def _fail_current(self, message: str) -> None:
        current = self._current
        if current is not None:
            self._error_count += 1
            self.streamer_error.emit(current.id, message)
        self._current = None
        QTimer.singleShot(200, self._load_next)
