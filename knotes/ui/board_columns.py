# SPDX-License-Identifier: GPL-3.0-only
"""A column of the task board: its cards in order, dragged with the mouse to another column or another place in this
one, opened with a click or Enter, archived with Delete; the "○ To do 3" header above it, whose name a double click
edits in place on a project board."""

import dataclasses
import re
from collections.abc import Callable

from PySide6.QtCore import QEvent, QMimeData, QObject, QPoint, QSize, Qt, Signal
from PySide6.QtGui import (
    QDrag,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QFocusEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidgetItem,
    QWidget,
)

from knotes import language, projects, tasks
from knotes.ui import style
from knotes.ui.board_card import CardLook, TaskCard
from knotes.ui.board_icons import state_sign, tone
from knotes.ui.board_style import CHOSEN, OPENED
from knotes.ui.viewport_list import ViewportList

TASK_MIME = "application/x-qg-knotes-task"  # a dragged task: its key
ROLE = Qt.ItemDataRole.UserRole
COLUMN_WIDTH = 250  # narrower columns scroll sideways


def _looks(task: tasks.Task) -> tasks.Task:
    """What a card shows: its place in the column and its column key are not on it."""
    return dataclasses.replace(task, order=None, column="")


class TaskColumn(ViewportList):
    """A board column: a card is dragged here from another column or to another place in this one; click and Enter
    open the card, Delete archives it."""

    moved_item = Signal(str, str, int)  # task key, this column's key and the place among its shown cards
    menu = Signal(object, QPoint)  # task and where to show its menu
    open_card = Signal(object)  # task
    to_archive = Signal(object)  # task
    open_place = Signal(object, float)  # source: meeting folder and second

    def __init__(self, column: projects.Column):
        super().__init__()
        self.column = column
        self.setObjectName("колонка_задач")
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSpacing(3)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._from_menu)
        self.itemClicked.connect(lambda element: self.open_card.emit(element.data(ROLE)))
        self.currentItemChanged.connect(lambda *_: self.mark())
        self._line_mark = QFrame(self.viewport())  # where the dragged card will land
        self._line_mark.setObjectName("черта")
        self._line_mark.hide()
        self._capture = QPoint(12, 12)  # where the mouse grabbed the card
        self._opened = ""  # key of the card open in the panel
        self._look: CardLook | None = None  # what the card widgets were made with
        self._found: re.Pattern | None = None  # what the board search lights up

    def set_(
        self, list_: list[tasks.Task], look: CardLook, visible: set[str] | None = None, limit: int | None = None
    ) -> int:
        """All cards of the column in order; visible: the keys a filter lets through, limit: how many of them to
        show. Only what changed is drawn: a filter hides rows, an edited card gets a new widget, the other cards
        stay; a card is made when it is first shown. The scroll position and the chosen card stay. Returns how many
        cards are shown."""
        scrolled = self.verticalScrollBar().value()
        chosen = self.currentItem().data(ROLE).key if self.currentItem() is not None else look.opened
        self._opened = look.opened
        common = dataclasses.replace(look, opened="")  # the open card is marked by mark(), not made again
        self.setUpdatesEnabled(False)
        self.blockSignals(True)
        if common != self._look:
            self._look = common
            self.clear()
        self._sync(list_)
        shown = self._show(visible, limit)
        self.setCurrentItem(next((e for e in self._shown() if e.data(ROLE).key == chosen), None))
        self.blockSignals(False)
        self.doItemsLayout()
        self.verticalScrollBar().setValue(scrolled)
        self.refresh_widgets()
        self.setUpdatesEnabled(True)
        self.mark()
        return shown

    def _sync(self, list_: list[tasks.Task]) -> None:
        """Rows to the cards in their order with few changes: a row keeps its card widget unless the card moved past
        the others or looks different now."""
        wanted = {z.key for z in list_}
        rows = [self.item(n).data(ROLE).key for n in range(self.count())]
        kept = tasks.kept(rows, [z.key for z in list_])
        for number in reversed(range(self.count())):
            if rows[number] not in wanted or rows[number] not in kept:
                self.forget_widget(self.item(number))
                self.takeItem(number)
        for number, task in enumerate(list_):
            if task.key not in kept:
                self.insertItem(number, QListWidgetItem())
            element = self.item(number)
            before = element.data(ROLE)
            if before == task:
                continue
            element.setData(ROLE, task)
            if before is None:
                element.setSizeHint(QSize(0, 84))  # measured when the card comes into view
            elif _looks(before) != _looks(task):
                self.forget_widget(element)

    def _show(self, visible: set[str] | None, limit: int | None) -> int:
        shown = 0
        for number in range(self.count()):
            element = self.item(number)
            task = element.data(ROLE)
            show = (visible is None or task.key in visible) and (limit is None or shown < limit)
            if element.isHidden() == show:
                element.setHidden(not show)
            if show:
                shown += 1
        return shown

    def _shown(self) -> list[QListWidgetItem]:
        return [e for e in (self.item(n) for n in range(self.count())) if not e.isHidden()]

    def _make_widget(self, item: QListWidgetItem) -> QWidget:
        widget = TaskCard(item.data(ROLE), self._look)
        widget.open_place.connect(self.open_place)
        if self._found is not None:
            widget.light_up(self._found)
        self._mark_item(item, widget)
        return widget

    def light_up(self, found: re.Pattern | None) -> None:
        """The search words in the shown cards; a card made later gets them when it is made."""
        self._found = found
        for element in self.mounted_items():
            self.itemWidget(element).light_up(found)

    def keys(self) -> list[str]:
        """Keys of the shown cards, top to bottom."""
        return [e.data(ROLE).key for e in self._shown()]

    def mark(self, opened: str | None = None) -> None:
        """An accent color border on the card open in the panel and on the one chosen with the keys."""
        if opened is not None:
            self._opened = opened
        for element in self.mounted_items():
            self._mark_item(element, self.itemWidget(element))

    def _mark_item(self, item: QListWidgetItem, widget: QWidget) -> None:
        opened = item.data(ROLE).key == self._opened
        chosen = item is self.currentItem() and self.hasFocus()
        if (widget.property(OPENED), widget.property(CHOSEN)) != (opened, chosen):
            widget.setProperty(OPENED, opened)
            widget.setProperty(CHOSEN, chosen)
            style.restyle(widget)

    def _fit_widget(self, item: QListWidgetItem, widget: QWidget) -> None:
        """Cards take the column width, the height follows the text wrapping."""
        width = max(120, self.viewport().width() - 8)
        if widget.minimumWidth() != width:
            widget.setFixedWidth(width)
        size = QSize(width, widget.heightForWidth(width))
        if item.sizeHint() != size:
            item.setSizeHint(size)

    def focusInEvent(self, event: QFocusEvent) -> None:
        super().focusInEvent(event)
        self.mark()

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        self.mark()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        task = self.currentItem().data(ROLE) if self.currentItem() is not None else None
        if task and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.open_card.emit(task)
        elif task and event.key() == Qt.Key.Key_Delete:
            self.to_archive.emit(task)
        else:
            super().keyPressEvent(event)

    def _from_menu(self, point: QPoint) -> None:
        element = self.itemAt(point)
        if element is not None:
            self.menu.emit(element.data(ROLE), self.viewport().mapToGlobal(point))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        element = self.itemAt(event.position().toPoint())
        if element is not None:
            self._capture = event.position().toPoint() - self.visualItemRect(element).topLeft()
        super().mousePressEvent(event)

    def mimeData(self, elements: list[QListWidgetItem]) -> QMimeData:
        data = QMimeData()
        data.setData(TASK_MIME, elements[0].data(ROLE).key.encode() if elements else b"")
        return data

    def startDrag(self, _actions: Qt.DropAction) -> None:
        """The card itself, semi-transparent, under the pointer."""
        elements = self.selectedItems()
        if not elements:
            return
        snapshot = self.mount_item(elements[0]).grab()
        pixmap = QPixmap(snapshot.size())
        pixmap.setDevicePixelRatio(snapshot.devicePixelRatio())
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setOpacity(0.85)
        painter.drawPixmap(0, 0, snapshot)
        painter.end()
        drag = QDrag(self)
        drag.setMimeData(self.mimeData(elements))
        drag.setPixmap(pixmap)
        drag.setHotSpot(self._capture)
        drag.exec(Qt.DropAction.MoveAction)
        self._line_mark.hide()

    def _place(self, point: QPoint) -> int:
        """Where the card will land among the shown ones: before the first card whose middle is below the pointer."""
        shown = self._shown()
        return next((n for n, e in enumerate(shown) if point.y() < self.visualItemRect(e).center().y()), len(shown))

    def _show_line(self, place: int) -> None:
        shown = self._shown()
        if not shown:
            y = 2
        elif place < len(shown):
            y = self.visualItemRect(shown[place]).top() - 2
        else:
            y = self.visualItemRect(shown[-1]).bottom() + 3
        self._line_mark.setGeometry(4, max(0, y - 1), self.viewport().width() - 8, 2)
        self._line_mark.show()
        self._line_mark.raise_()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasFormat(TASK_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        super().dragMoveEvent(event)  # scrolling at the column edge
        if event.mimeData().hasFormat(TASK_MIME):
            event.acceptProposedAction()
            self._show_line(self._place(event.position().toPoint()))

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._line_mark.hide()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        """The status and place change in the data and the board redraws itself: rows are not moved one by one."""
        self._line_mark.hide()
        key = bytes(event.mimeData().data(TASK_MIME)).decode()
        event.setDropAction(Qt.DropAction.CopyAction)  # otherwise the source column deletes the row itself
        event.accept()
        if key:
            self.moved_item.emit(key, self.column.key, self._place(event.position().toPoint()))


class ColumnTitle(QWidget):
    """The column name; with rename (a project board) a double click puts a field in its place: Enter or leaving the
    field saves, Esc leaves the name as it was. The board builds the header again after a rename."""

    def __init__(self, name: str, rename: Callable[[str], object] | None):
        super().__init__()
        self._rename = rename
        self.label = style.Elided(name, language.tr("Double-click to rename") if rename else "")
        self.label.setStyleSheet("font-weight: 600;")
        self.field = QLineEdit(name)
        self.field.hide()
        self.field.editingFinished.connect(self._finish)
        self.field.installEventFilter(self)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.label)
        row.addWidget(self.field)
        self.setMinimumHeight(self.field.sizeHint().height())  # the cards do not jump when the field shows

    def editing(self) -> bool:
        return not self.field.isHidden()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if self._rename is None:
            super().mouseDoubleClickEvent(event)
            return
        self.field.setText(self.label.full())
        self.label.hide()
        self.field.show()
        self.field.setFocus()
        self.field.selectAll()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.field and isinstance(event, QKeyEvent) and event.key() == Qt.Key.Key_Escape:
            self.field.setText(self.label.full())  # hiding the field finishes editing: nothing new to save
            self._show_name()
            return True
        return super().eventFilter(watched, event)

    def _show_name(self) -> None:
        self.field.hide()
        self.label.show()

    def _finish(self) -> None:
        if not self.editing():
            return
        self._show_name()
        if self._rename is not None and self.field.text().strip() != self.label.full():
            self._rename(self.field.text())


@dataclasses.dataclass(frozen=True)
class ColumnHeader:
    """The header above a column, with the state sign, the name and the card counter the board updates."""

    widget: QWidget
    sign: QLabel
    title: ColumnTitle
    counter: QLabel


def column_header(column: projects.Column, rename: Callable[[str], object] | None) -> ColumnHeader:
    """The "○ To do 3" header above the column: what the column counts as, its name, how many cards it shows."""
    header = QWidget()
    row = QHBoxLayout(header)
    row.setContentsMargins(4, 2, 4, 2)
    row.setSpacing(6)
    sign = QLabel()
    sign.setPixmap(state_sign(column.base, tone()))
    title = ColumnTitle(projects.column_name(column), rename)
    counter = QLabel()
    counter.setObjectName("второй_текст")
    for widget in (sign, title, counter):
        row.addWidget(widget)
    row.addStretch()
    return ColumnHeader(header, sign, title, counter)
