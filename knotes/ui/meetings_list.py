# SPDX-License-Identifier: GPL-3.0-only
"""The meeting list of the meetings window: rows follow the column width, selected meetings are dragged onto a project
as a small card under the pointer."""

from collections.abc import Callable

from PySide6.QtCore import QMimeData, QPoint, QRectF, QSize, Qt
from PySide6.QtGui import QDrag, QFontMetrics, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListWidgetItem, QWidget

from knotes.ui import style
from knotes.ui.viewport_list import ViewportList
from knotes.language import tr

MEETINGS_MIME = "application/x-qg-knotes-meetings"  # dragged meetings: folder paths separated by line breaks
DAY_ROLE = Qt.ItemDataRole.UserRole + 1


class MeetingsList(ViewportList):
    """Meeting list from which meetings can be dragged onto a project."""

    def __init__(self, make_row: Callable[[QListWidgetItem], QWidget]):
        super().__init__()
        self._make_row = make_row
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

    def _make_widget(self, item: QListWidgetItem) -> QWidget:
        if item.data(Qt.ItemDataRole.UserRole) is not None:
            return self._make_row(item)
        label = style.caption(item.data(DAY_ROLE))
        label.setObjectName("группа")
        return label

    def _fit_widget(self, item: QListWidgetItem, widget: QWidget) -> None:
        width = self.viewport().width()
        if widget.width() != width:
            widget.resize(width, widget.height())
        if item.data(Qt.ItemDataRole.UserRole) is not None:
            size = QSize(0, widget.sizeHint().height())
            if item.sizeHint() != size:
                item.setSizeHint(size)

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
