# SPDX-License-Identifier: GPL-3.0-only
"""Summary charts, drawn by hand without a charting library: bars of costs and meeting hours by step, share strips
of the breakdowns. A cost bar has two parts (speech and minutes), the current step is darker, hovering shows how much,
which model and what share."""

import math
from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPalette
from PySide6.QtWidgets import QToolTip, QWidget

from knotes import summary
from knotes.language import tr
from knotes.ui import style

CAPTION_PX = 40  # room a bar caption needs; when tighter, every second, third... bar is captioned
# by theme (light, dark): for each bar part (speech, minutes), (normal, current step)
BAR_COLORS = {
    False: (("#14958A", "#0F6E68"), ("#C27118", "#8A4F0E")),
    True: (("#1F9C90", "#5CC2B8"), ("#C47F2C", "#E3AC66")),
}


def kind_color(number: int) -> str:
    return BAR_COLORS[style.dark()][number][0]


def gray() -> str:
    """Strips of what is outside meetings and projects: the quiet text tone of the theme."""
    return style.text_tones()[1]


def legend() -> str:
    """The bar parts: a small square in the part's color next to its name."""
    return "&nbsp;&nbsp;&nbsp;".join(
        f'<span style="color:{kind_color(n)}">■</span> {tr(kind)}' for n, kind in enumerate(summary.KINDS)
    )


def scale_top(largest: float, divisions: int = 4) -> float:
    """Scale top made of round divisions (1, 2, 2,5 or 5 with zeros), not below the largest value."""
    if largest <= 0:
        return 1.0
    raw = largest / divisions
    order = 10 ** math.floor(math.log10(raw))
    step = next(k * order for k in (1, 2, 2.5, 5, 10) if k * order >= raw)
    return step * divisions


class Columns(QWidget):
    """Bar chart: a bar made of parts stacked on each other (hours have one part); the current step is darker."""

    def __init__(self, row_label: Callable[[float], str], axis: Callable[[float], str]):
        super().__init__()
        self.data: list[tuple[str, tuple[float, ...]]] = []
        self.current: int | None = None
        self.tips: list[str] = []
        self._row_label, self._axis = row_label, axis
        self.setMinimumHeight(190)
        self.setMouseTracking(True)

    def set_(
        self, data: list[tuple[str, tuple[float, ...]]], current: int | None = None, tips: list[str] | None = None
    ) -> None:
        """tips is the hover text for each bar; without them, the caption and the total."""
        self.data, self.current, self.tips = data, current, tips or []
        self.update()

    def _top(self) -> float:
        return scale_top(max((sum(z) for _, z in self.data), default=0))

    def _field(self) -> QRectF:
        return QRectF(44, 8, self.width() - 52, self.height() - 32)

    def paintEvent(self, _event_of: QPaintEvent) -> None:
        x_ = QPainter(self)
        x_.setRenderHint(QPainter.RenderHint.Antialiasing)
        field, top = self._field(), self._top()
        quiet = QColor(style.text_tones()[1])
        grid_layout = self.palette().color(QPalette.ColorRole.WindowText)
        grid_layout.setAlpha(28)
        x_.setFont(self.font())
        for division in range(5):
            y = field.bottom() - field.height() * division / 4
            x_.setPen(grid_layout)
            x_.drawLine(QPointF(field.left(), y), QPointF(field.right(), y))
            x_.setPen(quiet)
            right_ = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            x_.drawText(QRectF(0, y - 8, field.left() - 6, 16), right_, self._axis(top * division / 4))
        self._bars(x_, field, top)
        x_.end()

    def _bar(self, x_: QPainter, field: QRectF, left: float, width: float, parts: list[tuple[float, str]]) -> None:
        """Parts from the bottom up; only the bar top is rounded, parts are split by a gap in the background color."""
        parts = [(v, c_) for v, c_ in parts if v >= 1]
        if not parts:
            return
        background_, bottom = self.palette().window(), field.bottom()
        x_.setPen(Qt.PenStyle.NoPen)
        top = bottom - sum(v for v, _ in parts)
        x_.setBrush(QColor(parts[-1][1]))
        x_.drawRoundedRect(QRectF(left, top, width, parts[-1][0] + 4), 4, 4)
        for height, color in parts[:-1]:
            x_.fillRect(QRectF(left, bottom - height, width, height), QColor(color))
            bottom -= height
            x_.fillRect(QRectF(left, bottom - 1, width, 2), background_)
        x_.fillRect(QRectF(left, field.bottom(), width, 4), background_)

    def _bars(self, x_: QPainter, field: QRectF, top: float) -> None:
        if not self.data:
            return
        colors = BAR_COLORS[style.dark()]
        place = field.width() / len(self.data)
        width = min(24.0, place * 0.6)
        via = max(1, math.ceil(CAPTION_PX / place))
        for number, (row_label, values) in enumerate(self.data):
            center = field.left() + place * (number + 0.5)
            parts = [(field.height() * z / top, colors[k][number == self.current]) for k, z in enumerate(values)]
            self._bar(x_, field, center - width / 2, width, parts)
            if number % via:
                continue
            x_.setPen(QColor(style.text_tones()[1]))
            caption_width = max(place * via, CAPTION_PX)  # wider than its bar: the neighbors have no caption
            x_.drawText(
                QRectF(center - caption_width / 2, field.bottom() + 4, caption_width, 18),
                Qt.AlignmentFlag.AlignCenter,
                row_label,
            )

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        field = self._field()
        if not self.data or not field.contains(event.position()):
            QToolTip.hideText()
            return
        place = field.width() / len(self.data)
        number = min(len(self.data) - 1, int((event.position().x() - field.left()) / place))
        row_label, values = self.data[number]
        text = self.tips[number] if number < len(self.tips) else f"{row_label}: {self._row_label(sum(values))}"
        QToolTip.showText(event.globalPosition().toPoint(), style.plain_tooltip(text), self)  # model names: as text


class Strip(QWidget):
    """Horizontal share strip: a project in the breakdown."""

    def __init__(self, share: float, color: str):
        super().__init__()
        self.share, self.color = share, color
        self.setMinimumHeight(14)

    def paintEvent(self, _event_of: QPaintEvent) -> None:
        x_ = QPainter(self)
        x_.setRenderHint(QPainter.RenderHint.Antialiasing)
        x_.setPen(Qt.PenStyle.NoPen)
        x_.setBrush(QColor(self.color))
        width = max(3.0, self.width() * self.share)
        x_.drawRoundedRect(QRectF(0, 2, width, self.height() - 4), 3, 3)
        x_.end()
