from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..services.ai_provider import (
    MAX_PROBED_MODELS,
    AIModelHealth,
    AIModelOption,
    list_text_models,
    probe_model_health,
)


logger = logging.getLogger(__name__)

# Probing is mostly waiting on Google, and a stressed fleet can take ~30s per
# model, so run a few at once instead of serialising the whole shortlist.
_PROBE_WORKERS = 4


class AIModelListWorker(QObject):
    """Fetch the models this API key may select."""

    succeeded = Signal(list)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, api_key: str):
        super().__init__()
        self.api_key = api_key

    @Slot()
    def run(self) -> None:
        try:
            options: list[AIModelOption] = list_text_models(self.api_key)
        except Exception as error:
            logger.exception("Gemini model listing failed")
            self.failed.emit(" ".join(str(error).split())[:300])
        else:
            self.succeeded.emit(options)
        finally:
            self.finished.emit()


class AIModelHealthWorker(QObject):
    """Probe several models at once, reporting each as it answers."""

    probed = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, api_key: str, model_names: list[str]):
        super().__init__()
        self.api_key = api_key
        self.model_names = list(model_names)[:MAX_PROBED_MODELS]

    @Slot()
    def run(self) -> None:
        thread = QThread.currentThread()
        try:
            if not self.model_names:
                self.failed.emit("점검할 모델이 없습니다. 먼저 모델 목록을 불러오세요.")
                return
            with ThreadPoolExecutor(max_workers=_PROBE_WORKERS) as pool:
                futures = {
                    pool.submit(probe_model_health, self.api_key, name): name
                    for name in self.model_names
                }
                for future in as_completed(futures):
                    if thread.isInterruptionRequested():
                        for pending in futures:
                            pending.cancel()
                        break
                    try:
                        result: AIModelHealth = future.result()
                    except Exception as error:
                        logger.exception("Model probe crashed")
                        self.failed.emit(" ".join(str(error).split())[:300])
                        continue
                    self.probed.emit(result)
        finally:
            self.finished.emit()
