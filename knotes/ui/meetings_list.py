# SPDX-License-Identifier: GPL-3.0-only
"""The meeting list of the meetings window: rows follow the column width, selected meetings are dragged onto a project
as a small card under the pointer."""

from PySide6.QtCore import QMimeData, QPoint, QRectF, Qt
from PySide6.QtGui import QDrag, QFontMetrics, QPainter, QPainterPath, QPalette, QPen, QPixmap, QResizeEvent
from PySide6.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem, QWidget

from knotes.ui import style
from knotes.language import tr

MEETINGS_MIME = "application/x-qg-knotes-meetings"  # dragged meetings: folder paths separated by line breaks


class MeetingsList(QListWidget):
    """Meeting list from which meetings can be dragged onto a project."""

    def __init__(self):
        super().__init__()
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Rows follow the column width, also when it narrows: the row widgets do not follow the column themselves."""
        super().resizeEvent(event)
        width = self.viewport().width()
        for number in range(self.count()):
            line = self.itemWidget(self.item(number))
            if line is not None and line.width() != width:
                line.resize(width, line.height())

    def mimeData(self, elements: list[QListWidgetItem]) -> QMimeData:
        data = QMimeData()
        folders = [
            str(v.meeting.folder)
            for e_ in elements
            if (v := e_.data(Qt.ItemDataRole.UserRole)) and v.meeting.folder.is_dir()
        ]
        data.setData(MEETINGS_MIME, "\n".join(folders).encode())
        return data

    def startDrag(self, _actions: Qt.DropAction) -> None:
        """Under the pointer: a card "Meeting 21.10 22:52" or "Meetings: 3", not the whole selected row."""
        kinds = [
            v for e_ in self.selectedItems() if (v := e_.data(Qt.ItemDataRole.UserRole)) and v.meeting.folder.is_dir()
        ]
        if not kinds:
            return
        drag = QDrag(self)
        drag.setMimeData(self.mimeData(self.selectedItems()))
        pixmap = card(kinds[0].meeting.display_title if len(kinds) == 1 else tr("Meetings: {n}", n=len(kinds)), self)
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(-12, int(pixmap.height() / pixmap.devicePixelRatio() / 2)))
        drag.exec(Qt.DropAction.CopyAction)


def card(text: str, widget: QWidget) -> QPixmap:
    """A small translucent card with a folder icon and text: what follows the pointer."""
    font, icon = widget.font(), style.icon_font(14)
    width = QFontMetrics(font).horizontalAdvance(text) + 52
    height, density = 34, widget.devicePixelRatioF()
    pixmap = QPixmap(int(width * density), int(height * density))
    pixmap.setDevicePixelRatio(density)
    pixmap.fill(Qt.GlobalColor.transparent)
    x_ = QPainter(pixmap)
    x_.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0.5, 0.5, width - 1, height - 1), 8, 8)
    background_ = widget.palette().color(QPalette.ColorRole.Base)
    background_.setAlpha(235)
    x_.fillPath(path, background_)
    x_.setPen(QPen(style.accent(), 1.2))
    x_.drawPath(path)
    x_.setPen(style.accent())
    x_.setFont(icon)
    x_.drawText(QRectF(12, 0, 20, height), Qt.AlignmentFlag.AlignVCenter, style.FOLDER)
    x_.setPen(widget.palette().color(QPalette.ColorRole.Text))
    x_.setFont(font)
    x_.drawText(QRectF(38, 0, width - 44, height), Qt.AlignmentFlag.AlignVCenter, text)
    x_.end()
    return pixmap
