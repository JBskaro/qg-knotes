# SPDX-License-Identifier: GPL-3.0-only
"""Messages at the top of the screen in the recording bar style: a question with buttons or a short notice.

Used instead of Windows notifications: those have no buttons of their own, and in "Do not disturb" mode (full screen,
screen sharing) Windows does not show them at all. Toasts do not take focus and fade by themselves.
"""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout

from knotes.ui import style

FADE_AFTER_S = 20
WIDTH = 420


class Toast(style.Toast):
    """Signal choice(answer): the key of the pressed button, "нажато" for a click on a toast without buttons,
    "нет ответа" when it faded."""

    choice = Signal(str)

    def __init__(
        self,
        heading: str,
        text: str,
        buttons: list[tuple[str, str]],
        hide_from_capture: bool = True,
        fades_after: int = FADE_AFTER_S,
    ):
        super().__init__(hide_from_capture)
        self.setWindowTitle(f"{style.NAME} - {heading}")
        column = QVBoxLayout(self)
        column.setContentsMargins(18, 12, 18, 12)
        column.setSpacing(6)
        header = style.caption(heading)
        header.setStyleSheet("font-weight: 600; font-size: 15px;")
        column.addWidget(header)
        if text:
            hint = style.caption(text)
            hint.setObjectName("второй")
            hint.setWordWrap(True)
            column.addWidget(hint)
        if buttons:
            column.addLayout(self._buttons_row(buttons))
        else:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(WIDTH)
        self._with_buttons, self._answered = bool(buttons), False
        QTimer.singleShot(fades_after * 1000, lambda: self._answer("нет ответа"))

    def _buttons_row(self, buttons: list[tuple[str, str]]) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        for number, (key, row_label) in enumerate(buttons):
            button = QPushButton(row_label)
            button.setObjectName("главная" if number == 0 else "тихая" if number == len(buttons) - 1 else "")
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _pressed=False, k=key: self._answer(k))
            row.addWidget(button)
        row.addStretch()
        return row

    def fit_size(self) -> None:
        """Height fits the wrapped text at the toast width, with no gaps between lines."""
        self.ensurePolished()
        self.setFixedHeight(self.layout().totalHeightForWidth(WIDTH))
        self.layout().activate()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self._with_buttons:
            self._answer("нажато")

    def _answer(self, key: str) -> None:
        if self._answered:
            return
        self._answered = True
        self.close()
        self.choice.emit(key)
