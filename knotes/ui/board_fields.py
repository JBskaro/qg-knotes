# SPDX-License-Identifier: GPL-3.0-only
"""Edit fields of the task board: the quick add line at the bottom of a column, the wrapping text fields and the due
date field of the card panel."""

from collections.abc import Callable

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QIcon, QKeyEvent, QResizeEvent
from PySide6.QtWidgets import QComboBox, QMenu, QTextEdit

from knotes.ui import style
from knotes.ui.board_menus import cleaned


class AddField(style.ClearableLine):
    """The "+ Add task" line at the bottom of a column: Enter adds the task to this column, the field stays for the
    next one; the board clears it once the task is written."""

    add = Signal(str, str)  # text and column key

    def __init__(self, key: str, tooltip: str):
        super().__init__()
        self.key = key
        self.setObjectName("быстрая")
        self.setPlaceholderText(tooltip)
        self.returnPressed.connect(self._add)

    def _add(self) -> None:
        if text := self.text().strip():
            self.add.emit(text, self.key)


class TextField(QTextEdit):
    """Plain text in the card panel: wraps by words, the height follows the text within heights. In a line field
    (the heading), Enter moves on instead of starting a new line."""

    further = Signal()

    def __init__(self, name: str, tooltip: str, heights: tuple[int, int], line: bool):
        super().__init__()
        self.setObjectName(name)
        self._heights, self._line = heights, line
        self.setAcceptRichText(False)
        self.setTabChangesFocus(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setPlaceholderText(tooltip)
        self.document().setDocumentMargin(2 if line else 6)
        self.document().documentLayout().documentSizeChanged.connect(lambda _size_of: self._fit())

    def _fit(self) -> None:
        need = int(self.document().size().height()) + 2 * self.frameWidth() + 2
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if need > self._heights[1] else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.setFixedHeight(min(max(need, self._heights[0]), self._heights[1]))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._line and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.further.emit()
        else:
            super().keyPressEvent(event)


class DueField(QComboBox):
    """The due date in the panel: looks like the other fields, but opens a menu with a calendar instead of a list."""

    def __init__(self, collect: Callable[[QMenu], object]):
        super().__init__()
        self._menu, self._build = QMenu(self), collect

    def display(self, text: str) -> None:
        self.clear()
        self.addItem(QIcon(), text)

    def showPopup(self) -> None:
        self._build(cleaned(self._menu))
        self._menu.exec(self.mapToGlobal(QPoint(0, self.height())))
