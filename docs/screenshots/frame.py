# SPDX-License-Identifier: GPL-3.0-only
"""Pictures for the README in one style: window shots with rounded corners and a soft shadow on a calm gradient of
the program's teal. One shot per picture, or several overlapping windows for the page header.

Shots are used pixel for pixel, at the screen's own resolution: a scaled-down shot makes the program's text too small
to read on the page. scale is the screen's pixel ratio; margins, corners and shadows grow with it."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QRadialGradient

DEEP, NIGHT = QColor("#0B3F3C"), QColor("#0D1117")
RADIUS, MARGIN, SHADOW = 12, 28, 18  # in screen points, times scale in pixels


def _background(painter: QPainter, width: int, height: int) -> None:
    linear = QLinearGradient(0, 0, width, height)
    linear.setColorAt(0, DEEP)
    linear.setColorAt(1, NIGHT)
    painter.fillRect(0, 0, width, height, linear)
    glow = QRadialGradient(QPointF(width * 0.2, 0), width * 0.7)
    glow.setColorAt(0, QColor(15, 110, 104, 150))
    glow.setColorAt(1, QColor(15, 110, 104, 0))
    painter.fillRect(0, 0, width, height, glow)


def _window(painter: QPainter, shot: QImage, rect: QRectF, scale: float) -> None:
    """The shot in the rectangle: a soft shadow (stacked translucent shapes, QPainter has no blur), rounded corners
    and a thin light edge."""
    radius, steps = RADIUS * scale, int(SHADOW * scale)
    painter.setPen(Qt.PenStyle.NoPen)
    for step in range(steps, 0, -1):
        painter.setBrush(QColor(0, 0, 0, max(2, 120 // steps)))
        grown = rect.adjusted(-step, -step + steps / 3, step, step + steps / 3)
        painter.drawRoundedRect(grown, radius + step, radius + step)
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    painter.save()
    painter.setClipPath(path)
    painter.drawImage(rect, shot)
    painter.restore()
    painter.setPen(QColor(255, 255, 255, 40))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)


def compose(width: int, height: int, layers: list[tuple[QImage, QRectF]], scale: float = 1) -> QImage:
    """Windows drawn in order over the gradient, each into its rectangle (the same size as the shot: no scaling)."""
    canvas = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    _background(painter, width, height)
    for shot, rect in layers:
        _window(painter, shot, rect, scale)
    painter.end()
    return canvas


def framed(shot: QImage, scale: float = 1, title: str = "", subtitle: str = "") -> QImage:
    """One shot on the gradient with margins around it; a title and a subtitle go above it (the social preview)."""
    margin = int(MARGIN * scale)
    top = margin + (int(80 * scale) if title else 0)
    rect = QRectF(margin, top, shot.width(), shot.height())
    canvas = compose(shot.width() + 2 * margin, top + shot.height() + margin, [(shot, rect)], scale)
    if title:
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setPen(QColor("#FFFFFF"))
        title_font = QFont("Segoe UI Variable Display")
        title_font.setPixelSize(int(30 * scale))
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.drawText(
            QRectF(0, margin - 6 * scale, canvas.width(), 40 * scale), Qt.AlignmentFlag.AlignHCenter, title
        )
        painter.setPen(QColor(255, 255, 255, 190))
        subtitle_font = QFont("Segoe UI Variable Text")
        subtitle_font.setPixelSize(int(15 * scale))
        painter.setFont(subtitle_font)
        painter.drawText(
            QRectF(0, margin + 38 * scale, canvas.width(), 28 * scale), Qt.AlignmentFlag.AlignHCenter, subtitle
        )
        painter.end()
    return canvas
