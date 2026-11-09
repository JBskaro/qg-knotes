# SPDX-License-Identifier: GPL-3.0-only
"""Background jobs for windows: work runs in a thread pool, the result comes to the window thread by a signal."""

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from knotes import providers

log = logging.getLogger(__name__)
_live_tasks: set["_Job"] = set()  # jobs are kept until the result is delivered, or Python would collect them earlier


class _Signals(QObject):
    done = Signal(object)


def _deliver(on_result: Callable[[object], None], result: object) -> None:
    """The window waiting for the result may have been closed: then there is nobody to show it to."""
    try:
        on_result(result)
    except RuntimeError as error:
        if "deleted" not in str(error):
            raise
        log.info("background job result came to a closed window")


class _Job(QRunnable):
    def __init__(self, work: Callable[[], object], on_result: Callable[[object], None]):
        super().__init__()
        self.setAutoDelete(False)
        self.work = work
        self.signals = _Signals()
        self.signals.done.connect(lambda result: _deliver(on_result, result))
        self.signals.done.connect(lambda _result: _live_tasks.discard(self))

    def run(self) -> None:
        try:
            result = self.work()
        except providers.ProviderError as error:  # an expected failure with a clear text: no traceback
            log.warning("background job: %s", error)
            result = error
        except Exception as error:  # noqa: BLE001 - top of a background job: the window gets the error, it is not lost
            log.exception("background job")
            result = error
        try:
            self.signals.done.emit(result)
        except RuntimeError:  # the program is already closing, nobody to show the result to
            log.info("background job result after the program closed: %s", result)


def run_in_background(pool: QThreadPool, work: Callable[[], object], on_result: Callable[[object], None]) -> None:
    """The result of the work (or the exception if it failed) comes to on_result in the window thread."""
    task = _Job(work, on_result)
    _live_tasks.add(task)
    pool.start(task)
