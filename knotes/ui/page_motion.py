# SPDX-License-Identifier: GPL-3.0-only
"""A short fade for navigation. The new page receives input as soon as it is shown."""

from PySide6.QtCore import QEasingCurve, QEvent, QPropertyAnimation
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

from knotes import system

FADE_MS = 130


class _PageFade(QGraphicsOpacityEffect):
    def __init__(self, page: QWidget):
        super().__init__(page)
        self.setEnabled(False)
        self._animation = QPropertyAnimation(self, b"opacity", self)
        self._animation.setDuration(FADE_MS)
        self._animation.setStartValue(0.72)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.finished.connect(self._disable)
        page.installEventFilter(self)

    def start(self) -> None:
        self._animation.stop()
        self.setOpacity(0.72)
        self.setEnabled(True)
        self._animation.start()

    def _disable(self) -> None:
        self.setEnabled(False)

    def eventFilter(self, subject: QWidget, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Hide:
            self._animation.stop()
            self._disable()
        return super().eventFilter(subject, event)


def fade_in(page: QWidget) -> None:
    """Fade only a visible destination, honoring the Windows animation setting."""
    if not page.isVisible() or not system.client_animations_enabled():
        return
    parent = page.parentWidget()
    while parent is not None:
        parent_effect = parent.graphicsEffect()
        if isinstance(parent_effect, _PageFade) and parent_effect.isEnabled():
            return  # the outer page is already fading in
        parent = parent.parentWidget()
    effect = page.graphicsEffect()
    if effect is None:
        effect = _PageFade(page)
        page.setGraphicsEffect(effect)
    if isinstance(effect, _PageFade):
        effect.start()
