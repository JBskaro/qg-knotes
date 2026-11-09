# SPDX-License-Identifier: GPL-3.0-only
"""Signs of the task board drawn in code: priority, state and label color; the status, column and priority choices
with their signs."""

from collections.abc import Sequence

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF

from knotes import language, projects, tasks
from knotes.ui import style

URGENT, IN_PROGRESS = "#E5732E", "#D99A1E"  # color of the urgent priority sign and the "in progress" circle
LEVELS = {"низкий": 1, "средний": 2, "высокий": 3}  # how many bars of the priority sign are filled


def _canvas() -> tuple[QPixmap, QPainter]:
    """A 16×16 sign at double density: sharp on a scaled screen too."""
    pixmap = QPixmap(32, 32)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    return pixmap, painter


def priority_sign(priority: str, color: QColor) -> QPixmap:
    """As in Linear: urgent is an orange square with "!", the others are three bars filled by importance; no
    priority is three pale dashes."""
    pixmap, painter = _canvas()
    if priority == "срочный":
        painter.setBrush(QColor(URGENT))
        painter.drawRoundedRect(QRectF(1.5, 1.5, 13, 13), 3, 3)
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawRoundedRect(QRectF(7, 4, 2, 5.5), 1, 1)
        painter.drawEllipse(QRectF(7, 10.5, 2, 2))
    else:
        level = LEVELS.get(priority, 0)
        for number in range(3):
            tone_ = QColor(color)
            tone_.setAlphaF(1.0 if number < level else 0.3)
            painter.setBrush(tone_)
            height = 4 + 3.5 * number if level else 1.6
            painter.drawRoundedRect(QRectF(2.5 + 4 * number, 13 - height if level else 7.2, 3, height), 0.8, 0.8)
    painter.end()
    return pixmap


def state_sign(status: str, color: QColor) -> QPixmap:
    """An empty circle for "To do", a half filled one for "in progress", a filled one with a check mark for "done"."""
    pixmap, painter = _canvas()
    circle = QRectF(2.5, 2.5, 11, 11)
    if status == tasks.DONE:
        painter.setBrush(style.accent())
        painter.drawEllipse(circle)
        painter.setPen(
            QPen(QColor("#FFFFFF"), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        )
        painter.drawPolyline(QPolygonF([QPointF(5.4, 8.2), QPointF(7.3, 10), QPointF(10.6, 6.2)]))
    else:
        tone_ = QColor(IN_PROGRESS) if status == tasks.IN_PROGRESS else color
        painter.setPen(QPen(tone_, 1.5))
        painter.drawEllipse(circle)
        if status == tasks.IN_PROGRESS:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(tone_)
            painter.drawPie(QRectF(5, 5, 6, 6), 90 * 16, -180 * 16)
    painter.end()
    return pixmap


def color_sign(color: int) -> QPixmap:
    pixmap, painter = _canvas()
    painter.setBrush(QColor(style.participant_color(color)[0]))
    painter.drawEllipse(QRectF(3.5, 3.5, 9, 9))
    painter.end()
    return pixmap


def tone() -> QColor:
    """The calm text color the signs are drawn in."""
    return QColor(style.text_tones()[0])


def state_options() -> list[tuple[str, str, QIcon]]:
    return [(s, language.tr(p), QIcon(state_sign(s, tone()))) for s, p in tasks.STATES.items()]


def column_options(columns: Sequence[projects.Column]) -> list[tuple[str, str, QIcon]]:
    """Columns of a project board with the sign of their base; the value is the column key."""
    return [(c.key, projects.column_name(c), QIcon(state_sign(c.base, tone()))) for c in columns]


def priority_options() -> list[tuple[str, str, QIcon]]:
    return [(p, language.tr(row_label), QIcon(priority_sign(p, tone()))) for p, row_label in tasks.PRIORITIES.items()]
