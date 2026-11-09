# SPDX-License-Identifier: GPL-3.0-only
"""Chips of the task board: a label in its color or an assignee in gray, and chips in a wrapping row, on the card and
in the card panel."""

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLabel, QLayout, QLayoutItem, QWidget

from knotes.ui import style


def chip(text: str, color: int | None) -> QLabel:
    """A label in its color, an assignee (color None) in calm gray; a long name is elided."""
    caption = QLabel()
    caption.setTextFormat(Qt.TextFormat.PlainText)
    visible = caption.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, 150)
    caption.setText(visible)
    caption.setToolTip(style.plain_tooltip(text) if visible != text else "")
    text_color, background_ = style.participant_color(color) if color is not None else style.state_color("ждет")
    # radius at most half the height, otherwise Qt does not round at all
    caption.setStyleSheet(
        f"color: {text_color}; background: {background_}; border-radius: 7px; padding: 1px 7px; font-size: 11px;"
    )
    return caption


class ChipRow(QLayout):
    """Chips in a wrapping row, like words in a paragraph: labels on the card and in the panel."""

    def __init__(self, parent_: QWidget | None = None):
        super().__init__(parent_)
        self._elements: list[QLayoutItem] = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(4)

    def addItem(self, element: QLayoutItem) -> None:
        self._elements.append(element)

    def count(self) -> int:
        return len(self._elements)

    def itemAt(self, number: int) -> QLayoutItem | None:
        return self._elements[number] if 0 <= number < len(self._elements) else None

    def takeAt(self, number: int) -> QLayoutItem | None:
        return self._elements.pop(number) if 0 <= number < len(self._elements) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._lay_out(QRect(0, 0, width, 0), arrange=False)

    def setGeometry(self, area: QRect) -> None:
        super().setGeometry(area)
        self._lay_out(area, arrange=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for element in self._elements:
            size = size.expandedTo(element.minimumSize())
        return size

    def _lay_out(self, area: QRect, arrange: bool) -> int:
        """Height of the rows at this width; with arrange, also puts the chips in place."""
        x, y, height = area.x(), area.y(), 0
        for element in self._elements:
            size = element.sizeHint()
            if x + size.width() > area.right() + 1 and x > area.x():
                x, y, height = area.x(), y + height + self.spacing(), 0
            if arrange:
                element.setGeometry(QRect(QPoint(x, y), size))
            x += size.width() + self.spacing()
            height = max(height, size.height())
        return y + height - area.y()
