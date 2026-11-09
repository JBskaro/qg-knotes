# SPDX-License-Identifier: GPL-3.0-only
"""The recording bar at the top of the screen: dot, time, volume of the microphone and others, mark, pause, "Stop".

Visible the whole recording; collapses into a small toast "● 12:34". Does not take focus,
is dragged with the mouse; warnings appear right on it, an important one expands the collapsed bar.
"""

import math

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPaintEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from knotes import transcript
from knotes.ui import style
from knotes.language import tr

QUIET_DB, FULL_DB = -42.0, -6.0  # a quieter peak: empty bars, a louder one: full height
QUIET = 0.08  # below this share the bars are gray: only noise in the microphone
BAR_COUNT = 4
FRAME_MS = 33


def loudness_share(peak: int) -> float:
    """Track peak to a share of the bar height by decibels, like volume meters: quiet speech is visible, loud speech
    does not hit the ceiling."""
    if peak <= 0:
        return 0.0
    db = 20 * math.log10(peak / 32768)
    return min(1.0, max(0.0, (db - QUIET_DB) / (FULL_DB - QUIET_DB)))


def step_to(shown: float, target: float) -> float:
    """One frame of the bars: up fast, down smoothly, so the level does not jump between readings."""
    if target > shown:
        return shown + (target - shown) * 0.6
    return max(target, shown - max(0.02, (shown - target) * 0.12))


class Bars(QWidget):
    """Volume as four bars, like a signal indicator: quiet means low and gray."""

    def __init__(self):
        super().__init__()
        self.share = self._target = 0.0
        self._frames = QTimer(self)
        self._frames.setInterval(FRAME_MS)
        self._frames.timeout.connect(self._frame)

    def set_(self, share: float) -> None:
        self._target = share
        if not self._frames.isActive():
            self._frames.start()

    def _frame(self) -> None:
        self.share = step_to(self.share, self._target)
        if abs(self.share - self._target) < 0.005:
            self.share = self._target
            self._frames.stop()
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(BAR_COUNT * 5, 16)

    def paintEvent(self, _event_of: QPaintEvent) -> None:
        x_ = QPainter(self)
        x_.setRenderHint(QPainter.RenderHint.Antialiasing)
        x_.setPen(Qt.PenStyle.NoPen)
        silent = self.share < QUIET
        x_.setBrush(QColor("#5F5E5A") if silent else QColor("#97C459"))
        for number in range(BAR_COUNT):
            height = 3 if silent else 3 + 13 * min(1.0, self.share * (1.15 - number * 0.12))
            x_.drawRoundedRect(QRectF(number * 5, 16 - height, 3, height), 1, 1)
        x_.end()


class Level(QWidget):
    """Track icon and volume bars."""

    def __init__(self, sign: str, row_label: str):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        label = QLabel(sign)
        label.setObjectName("значок")
        self.bars = Bars()
        self.setAccessibleName(row_label)
        self.setToolTip(row_label)
        row.addWidget(label)
        row.addWidget(self.bars)

    def set_(self, peak: int | None) -> None:
        self.setVisible(peak is not None)
        self.bars.set_(loudness_share(peak or 0))


class RecordingBar(style.DraggedToast):
    """Signals: stop, mark, note (show or hide the meeting notepad), pause(yes/no), moved(point), collapsed(yes/no)."""

    stop = Signal()
    mark = Signal()
    note = Signal()
    pause = Signal(bool)
    moved = Signal(QPoint)
    collapsed = Signal(bool)

    def __init__(
        self, hide_from_capture: bool = True, key_seq: str = "", mark_hotkey: str = "", notes_hotkey: str = ""
    ):
        super().__init__(hide_from_capture)
        self.setWindowTitle(tr("{name} - recording", name=style.NAME))
        self._point = QLabel(style.DOT)
        self._point.setObjectName("точка")
        self._time = QLabel(tr("Recording {time}", time="00:00"))
        self._time.setObjectName("время")
        self.microphone = Level(style.MICROPHONE, tr("Your microphone"))
        self.others = Level(style.HEADPHONES, tr("Other participants: computer audio"))
        self._screen = QLabel(style.VIDEO)
        self._screen.setObjectName("значок")
        self._screen.setToolTip(tr("The meeting screen is being recorded"))
        self._screen.hide()
        self._stop = QPushButton(tr("Stop"))
        self._stop.setObjectName("стоп")
        self._stop.setToolTip(tr("Stop recording ({key_seq})", key_seq=key_seq) if key_seq else tr("Stop recording"))
        self._stop.clicked.connect(self.stop)
        self._collapse = QPushButton(style.COLLAPSE)
        self._collapse.setObjectName("значок")
        self._collapse.clicked.connect(lambda: self.collapse(not self.is_collapsed))
        self._mark = QPushButton(style.STAR)
        self._mark.setObjectName("значок")
        self._mark.setToolTip(
            tr("Mark an important moment: a star appears here in the transcript, so it's easy to find")
            + (f" ({mark_hotkey})" if mark_hotkey else "")
        )
        self._mark.clicked.connect(self.mark)
        self._note = QPushButton(style.NOTE)
        self._note.setObjectName("значок")
        self._notes_hotkey = notes_hotkey
        self._note.clicked.connect(self.note)
        self._note_tooltip(hide_from_capture)
        self._pause = QPushButton(style.PAUSE)
        self._pause.setObjectName("значок")
        self._pause.setToolTip(tr("Pause: the break won't be recorded"))
        self._pause.clicked.connect(lambda: self.pause.emit(not self.paused))
        self.paused = False
        for button in (self._stop, self._collapse, self._mark, self._note, self._pause):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._warning = QLabel()
        self._warning.setObjectName("предупреждение")
        self._warning.setWordWrap(True)
        self._warning.hide()
        self._warnings: dict[str, str] = {}

        self.is_collapsed = False
        self.records_screen = False
        self._seconds = 0.0
        self._device_names: dict[str, str] = {}

        self._build()
        self.collapse(False)

    def _build(self) -> None:
        row = QHBoxLayout()
        row.setSpacing(10)
        for widget in (self._point, self._time, self.microphone, self.others, self._screen):
            row.addWidget(widget)
        row.addStretch()
        for button in (self._mark, self._note, self._pause, self._stop, self._collapse):
            row.addWidget(button)
        column = QVBoxLayout(self)
        column.setContentsMargins(14, 8, 8, 8)
        column.setSpacing(4)
        column.addLayout(row)
        column.addWidget(self._warning)

    def hide_from_capture(self, hide_: bool) -> None:
        super().hide_from_capture(hide_)
        if getattr(self, "_note", None) is not None:  # the base class calls it before the buttons exist
            self._note_tooltip(hide_)

    def _note_tooltip(self, hidden: bool) -> None:
        """Participants can't see the notepad only while hiding from screen sharing is on in the settings."""
        text = (
            tr("Meeting notepad: agenda and your notes, each line gets the recording minute. Participants can't see it")
            if hidden
            else tr("Meeting notepad: agenda and your notes, each line gets the recording minute")
        )
        self._note.setToolTip(text + (f" ({self._notes_hotkey})" if self._notes_hotkey else ""))

    def collapse(self, collapse: bool) -> None:
        """A collapsed bar shows only the dot and time; the center stays in place."""
        center = self.geometry().center().x()
        self.is_collapsed = collapse
        for widget in (self.microphone, self.others, self._stop, self._mark, self._note, self._pause):
            widget.setVisible(not collapse)
        self._screen.setVisible(self.records_screen and not collapse)
        self._warning.setVisible(not collapse and bool(self._warnings))
        self._collapse.setText(style.EXPAND if collapse else style.COLLAPSE)
        self._collapse.setToolTip(tr("Expand") if collapse else tr("Collapse"))
        self.setMinimumWidth(0 if collapse else 460)
        self.adjustSize()
        self.resize(self.minimumSizeHint())
        if self.isVisible():
            self.move(center - self.width() // 2, self.y())
        self.collapsed.emit(collapse)

    def show_screen_icon(self, on: bool) -> None:
        """Screen recording icon: visible while the screen is recorded."""
        self.records_screen = on
        self._screen.setVisible(on and not self.is_collapsed)

    def label_devices(self, names: dict[str, str]) -> None:
        """Tooltips at the volume: which device is recorded."""
        if names == self._device_names:
            return
        self._device_names = dict(names)
        self.microphone.setToolTip(
            tr("Microphone: {device}", device=names["микрофон"])
            if "микрофон" in names
            else tr("Microphone not recorded")
        )
        self.others.setToolTip(
            tr("Other participants: {device}", device=names.get("собеседники", "").removesuffix(" [Loopback]"))
        )

    def set_pause(self, pause: bool) -> None:
        self.paused = pause
        self._pause.setText(style.LATER_SPAN if pause else style.PAUSE)
        self._pause.setToolTip(tr("Resume recording") if pause else tr("Pause: the break won't be recorded"))
        self._point.setObjectName("точка_пауза" if pause else "точка")
        self._point.setStyleSheet(self._point.styleSheet())  # reapply the style by the new name
        self.refresh(self._seconds, {})

    def refresh(self, seconds: float, levels: dict[str, int]) -> None:
        self._seconds = seconds
        time = transcript.time(seconds)
        row_label = tr("Paused {time}", time=time) if self.paused else tr("Recording {time}", time=time)
        self._time.setText(time if self.is_collapsed else row_label)
        if not self.is_collapsed:
            self.microphone.set_(levels.get("микрофон"))
            self.others.set_(levels.get("собеседники"))
        self._point.setVisible(self.paused or int(seconds * 2) % 4 != 3)  # calm blinking, not flicker

    def warn(self, key: str, text: str, seconds: int = 0, expand: bool = False) -> None:
        """A warning on the bar; with seconds it goes away by itself; an important one expands the collapsed bar when
        it appears, repeating it does not: the user may collapse the bar again."""
        new_one = key not in self._warnings
        if self._warnings.get(key) != text:
            self._warnings[key] = text
            self._show_warnings()
        if expand and new_one and self.is_collapsed:
            self.collapse(False)
        if seconds:
            QTimer.singleShot(seconds * 1000, lambda: self.remove(key))

    def remove(self, key: str) -> None:
        if self._warnings.pop(key, None) is not None:
            self._show_warnings()

    def _show_warnings(self) -> None:
        text = "\n".join(self._warnings.values())
        self._warning.setText(text)
        self._warning.setVisible(bool(text) and not self.is_collapsed)
        self.adjustSize()

    def place_item(self, point: list[int] | None) -> None:
        """To the saved place if it is on one of the screens; otherwise to the top center of the primary screen."""
        if point and any(e_.availableGeometry().contains(QPoint(*point)) for e_ in QGuiApplication.screens()):
            self.adjustSize()
            self.move(QPoint(*point))
            return
        self.top_center()

    def dropped(self) -> None:
        self.moved.emit(self.pos())
