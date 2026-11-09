# SPDX-License-Identifier: GPL-3.0-only
"""First-run tips: a card with an arrow pointing at a control of its window, one step after another."""

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPaintEvent, QPixmap, QPolygonF
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from knotes.ui import style
from knotes.language import tr

WIDTH = 320
BEAK = 10  # arrow length
GAP = 2  # between the arrow tip and the control
EDGE = 8  # the tip keeps this far inside its window
SIDES = ("top", "left", "bottom", "right")  # side of the tip the arrow is on, in order of preference
MARGINS = (16, 12, 12, 14)  # left, top, right, bottom


@dataclass(frozen=True)
class Step:
    title: str
    text: str
    target: Callable[[], QRect | None] = lambda: None  # control in the tour's window coordinates; None - centered
    icon: QPixmap | None = None
    action: str = ""  # main button of the last step instead of "Done"


def area_of(widget: QWidget, window: QWidget, part: QRect | None = None) -> QRect | None:
    """Rectangle of a widget (or of a part of it, in its coordinates) in window coordinates; None while hidden."""
    rect = widget.rect() if part is None else part
    if not widget.isVisibleTo(window) or rect.isEmpty():
        return None
    return QRect(widget.mapTo(window, rect.topLeft()), rect.size())


def _spot(target: QRect, side: str, size: tuple[int, int]) -> tuple[str, int, int, int, int]:
    width, height = size
    x, y = {
        "top": (target.center().x() - width // 2, target.bottom() + GAP),
        "bottom": (target.center().x() - width // 2, target.top() - GAP - height),
        "left": (target.right() + GAP, target.center().y() - height // 2),
        "right": (target.left() - GAP - width, target.center().y() - height // 2),
    }[side]
    return side, x, y, width, height


def _fits(spot: tuple[str, int, int, int, int], area: QRect) -> bool:
    side, x, y, width, height = spot
    if side in ("top", "bottom"):
        return area.top() <= y and y + height <= area.bottom()
    return area.left() <= x and x + width <= area.right()


def place(target: QRect | None, size: Callable[[str], tuple[int, int]], area: QRect) -> tuple[str, QRect, int]:
    """Side of the arrow, the tip's rectangle and where the arrow is along that side: below the control, else to its
    right, above, to its left. The tip stays inside area; without a target it is centered and has no arrow."""
    if target is None:
        width, height = size("")
        return "", QRect(area.center().x() - width // 2, area.center().y() - height // 2, width, height), 0
    spots = [_spot(target, side, size(side)) for side in SIDES]
    side, x, y, width, height = next((spot for spot in spots if _fits(spot, area)), spots[-1])
    x = max(area.left(), min(x, area.right() - width))
    y = max(area.top(), min(y, area.bottom() - height))
    along = target.center().x() - x if side in ("top", "bottom") else target.center().y() - y
    return side, QRect(x, y, width, height), along


class Tour(QWidget):
    """Tips over the window they belong to. finished(how): «done», «action» (the last step's main button) or
    «closed» (the cross)."""

    finished = Signal(str)

    def __init__(self, window: QWidget, steps: list[Step]):
        super().__init__(window)
        self._window, self._steps, self._index = window, steps, 0
        self._side, self._along = "", 0
        shadow = QGraphicsDropShadowEffect(self, blurRadius=28, offset=QPointF(0, 4))
        shadow.setColor(QColor(0, 0, 0, 90))
        self.setGraphicsEffect(shadow)
        self._title, self._text, self._icon, self._counter = QLabel(), QLabel(), QLabel(), QLabel()
        for label in (self._title, self._text):  # a tip may name a model from Hugging Face: as text, not markup
            label.setTextFormat(Qt.TextFormat.PlainText)
        self._title.setStyleSheet("font-weight: 600; font-size: 14px;")
        self._text.setWordWrap(True)
        self._counter.setObjectName("тихий_текст")
        self._back, self._next = QPushButton(tr("Back")), QPushButton()
        self._next.setObjectName("главная")
        for button in (self._back, self._next):
            button.setAutoDefault(False)
        self._back.clicked.connect(lambda: self.show_step(self._index - 1))
        self._next.clicked.connect(self._forward)
        self._close = style.icon_button(style.CLOSE, tr("Close tips"), lambda: self.finish("closed"))
        self._build()
        window.installEventFilter(self)

    def _build(self) -> None:
        head = QHBoxLayout()
        head.addWidget(self._title, 1)
        head.addWidget(self._close)
        body = QHBoxLayout()
        body.setSpacing(12)
        body.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)
        body.addWidget(self._text, 1)
        foot = QHBoxLayout()
        foot.addWidget(self._counter)
        foot.addStretch()
        foot.addWidget(self._back)
        foot.addWidget(self._next)
        column = QVBoxLayout(self)
        column.setSpacing(8)
        column.addLayout(head)
        column.addLayout(body)
        column.addSpacing(4)
        column.addLayout(foot)

    def start(self) -> None:
        self.show_step(0)
        self.show()
        self.raise_()

    def show_step(self, index: int) -> None:
        self._index = index
        step = self._steps[index]
        self._title.setText(step.title)
        self._text.setText(step.text)
        self._icon.setVisible(step.icon is not None)
        if step.icon is not None:
            self._icon.setPixmap(step.icon)
        last = index == len(self._steps) - 1
        self._counter.setText(tr("{number} of {total}", number=index + 1, total=len(self._steps)))
        self._back.setVisible(index > 0)
        self._next.setText((step.action or tr("Done")) if last else tr("Next"))
        self._place()

    def _forward(self) -> None:
        if self._index < len(self._steps) - 1:
            self.show_step(self._index + 1)
        else:
            self.finish("action" if self._steps[self._index].action else "done")

    def finish(self, how: str) -> None:
        """Hides the tips; the window owns them and deletes them with itself (deleteLater here crashed when the
        window went first)."""
        self._window.removeEventFilter(self)
        self.hide()
        self.finished.emit(how)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._window and event.type() == QEvent.Type.Resize:
            self._place()
        return False

    def _size(self, side: str) -> tuple[int, int]:
        margins: list[int] = list(MARGINS)
        if side:
            margins[("left", "top", "right", "bottom").index(side)] += BEAK
        self.layout().setContentsMargins(*margins)
        width = WIDTH + (BEAK if side in ("left", "right") else 0)
        return width, self.layout().totalHeightForWidth(width)

    def _place(self) -> None:
        area = self._window.rect().adjusted(EDGE, EDGE, -EDGE, -EDGE)
        self._side, rect, self._along = place(self._steps[self._index].target(), self._size, area)
        self._size(self._side)  # margins of the side the arrow ended up on
        self.setGeometry(rect)
        self.update()

    def _outline(self) -> QPainterPath:
        card = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self._side:
            card = card.adjusted(
                *{
                    "left": (BEAK, 0, 0, 0),
                    "top": (0, BEAK, 0, 0),
                    "right": (0, 0, -BEAK, 0),
                    "bottom": (0, 0, 0, -BEAK),
                }[self._side]
            )
        path = QPainterPath()
        path.addRoundedRect(card, 8, 8)
        if not self._side:
            return path
        length = self.width() if self._side in ("top", "bottom") else self.height()
        at = max(16.0, min(float(self._along), length - 16.0))
        points = {
            "top": [(at - BEAK, BEAK + 1), (at, 0.5), (at + BEAK, BEAK + 1)],
            "bottom": [(at - BEAK, card.bottom() - 1), (at, self.height() - 0.5), (at + BEAK, card.bottom() - 1)],
            "left": [(BEAK + 1, at - BEAK), (0.5, at), (BEAK + 1, at + BEAK)],
            "right": [(card.right() - 1, at - BEAK), (self.width() - 0.5, at), (card.right() - 1, at + BEAK)],
        }
        beak = QPainterPath()
        beak.addPolygon(QPolygonF([QPointF(x, y) for x, y in points[self._side]]))
        beak.closeSubpath()
        return path.united(beak)

    def paintEvent(self, _event: QPaintEvent) -> None:
        dark = style.dark()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        outline = self._outline()
        painter.fillPath(outline, QColor("#2C2C2C") if dark else QColor("#FFFFFF"))
        painter.setPen(QColor(255, 255, 255, 26) if dark else QColor(0, 0, 0, 30))
        painter.drawPath(outline)
        painter.end()
