# SPDX-License-Identifier: GPL-3.0-only
"""The widget of one meeting in the meeting list; what it shows comes from meeting_rows."""

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QPalette, QResizeEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from knotes import catalog
from knotes.meeting_rows import MeetingItem
from knotes.ui import style
from knotes.language import tr


class MeetingRow(QWidget):
    """A meeting in the list: title and project, summary, state, main action, the "…" menu."""

    action = Signal()
    menu = Signal(QPoint)

    def __init__(self, kind: MeetingItem, project_color: int | None = None):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._button: QPushButton | None = None  # send or retry: its words go when the row gets narrow
        self._words = ""
        v = kind.meeting
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 8, 8)
        row.setSpacing(10)
        row.addLayout(self._texts(kind, project_color), 1)
        if (
            kind.occupied
            or v.status == catalog.RECOGNITION_ERROR
            or (v.status != catalog.READY and not kind.recognizable)
        ):  # "Ready" and "Transcribe" speak for themselves
            text, color = kind.status
            status = QLabel(text)
            status.setStyleSheet(style.chip(color))
            status.setToolTip(style.plain_tooltip(v.error))
            row.addWidget(status, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._main_point(kind), 0, Qt.AlignmentFlag.AlignVCenter)
        more = style.icon_button(style.MORE, tr("More"))
        more.clicked.connect(lambda: self.menu.emit(more.mapToGlobal(more.rect().bottomLeft())))
        row.addWidget(more)
        self._wide = self.minimumSizeHint().width()  # the row fits with the words on the button from this width

    @staticmethod
    def _texts(kind: MeetingItem, project_color: int | None) -> QVBoxLayout:
        """Title and project, the summary, the minutes result and the progress bar."""
        v = kind.meeting
        title = style.Elided(v.display_title)  # in a narrow column the title is shortened instead of cutting buttons
        title.setObjectName("заголовок_встречи")
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(title)
        if v.project:
            project = style.Elided(v.project)
            project.setStyleSheet(style.project_chip(project_color))
            project.setMaximumWidth(project.sizeHint().width() + 24)
            project.setMinimumWidth(min(project.sizeHint().width() + 24, 100))  # a short project name stays whole
            top.addWidget(project, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addStretch()
        summary = catalog.summary(v) + (" · " + tr("found: {n}", n=kind.found) if kind.found else "")
        row_label = style.Elided(summary)
        row_label.setObjectName("второй_текст")
        texts = QVBoxLayout()
        texts.setSpacing(2)
        texts.addLayout(top)
        texts.addWidget(row_label)
        if kind.result:
            result_line = style.Elided(kind.result)
            result_line.setObjectName("тихий_текст")
            texts.addWidget(result_line)
        if kind.progress and kind.progress.total:
            bar = QProgressBar()
            bar.setObjectName("ход")
            bar.setRange(0, kind.progress.total)
            bar.setValue(kind.progress.done_count)
            bar.setTextVisible(False)
            bar.setFixedSize(220, 3)
            texts.addWidget(bar)
        return texts

    def resizeEvent(self, event: QResizeEvent) -> None:
        """In a narrow column the button shows only its icon, the words stay in the tooltip: otherwise it covers the
        title and the project."""
        super().resizeEvent(event)
        if self._button is not None:
            self._button.setText(self._words if self.width() >= self._wide else "")

    def _main_point(self, kind: MeetingItem) -> QWidget:
        """Button for what to do with the meeting: send or retry; for a busy one, how much is left.
        A ready one opens by a click on the row, it needs no button."""
        v, color = kind.meeting, self.palette().color(QPalette.ColorRole.ButtonText)
        if kind.occupied:
            remaining = QLabel(kind.progress.remaining() if kind.progress else "")
            remaining.setObjectName("тихий_текст")
            remaining.setMinimumWidth(130)
            return remaining
        if v.status == catalog.READY:
            return QLabel()
        if v.status == catalog.RECOGNITION_ERROR:
            button = QPushButton(style.icon(style.REFRESH, color), "  " + tr("Retry"))
        elif kind.recognizable:
            button = QPushButton(style.icon(style.SEND, color), "  " + tr("Transcribe"))
            button.setToolTip(
                tr("Send for transcription, about {price}", price=catalog.money(kind.price))
                if kind.price
                else tr("Send for transcription")
            )
        else:
            return QLabel()
        button.clicked.connect(self.action)
        button.setToolTip(button.toolTip() or button.text().strip())  # the icon alone in a narrow row
        self._button, self._words = button, button.text()
        return button
