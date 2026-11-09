# SPDX-License-Identifier: GPL-3.0-only
"""Windows that appear on screen while a page is built: a widget shown before it has a parent flashes as a window of
its own for a moment."""

import contextlib
from collections.abc import Generator

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QWidget


class _Watcher(QObject):
    def __init__(self):
        super().__init__()
        self.windows: list[str] = []

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt name
        if event.type() == QEvent.Type.Show and isinstance(watched, QWidget) and watched.isWindow():
            self.windows.append(type(watched).__name__)
        return False


@contextlib.contextmanager
def shown_windows() -> Generator[list[str]]:
    """Class names of the windows shown inside the block."""
    program = QApplication.instance()
    watcher = _Watcher()
    program.installEventFilter(watcher)
    try:
        yield watcher.windows
    finally:
        program.removeEventFilter(watcher)
