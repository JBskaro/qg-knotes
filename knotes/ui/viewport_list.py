# SPDX-License-Identifier: GPL-3.0-only
"""Keep item data in the list and create row widgets near its viewport."""

from PySide6.QtCore import QPoint, QTimer
from PySide6.QtGui import QResizeEvent, QShowEvent
from PySide6.QtWidgets import QApplication, QListWidget, QListWidgetItem, QWidget

SMALL_LIST = 14
BUFFER = 120


class ViewportList(QListWidget):
    def __init__(self):
        super().__init__()
        self._widgets: dict[int, tuple[QListWidgetItem, QWidget]] = {}  # QListWidgetItem is not hashable
        self._update_rows = QTimer(self, singleShot=True, interval=0)
        self._update_rows.timeout.connect(self.refresh_widgets)
        self.verticalScrollBar().valueChanged.connect(self._schedule_widgets)
        self.verticalScrollBar().rangeChanged.connect(self._schedule_widgets)

    def clear(self) -> None:
        self._widgets.clear()
        super().clear()

    def forget_widget(self, item: QListWidgetItem) -> None:
        if mounted := self._widgets.pop(id(item), None):
            mounted[1].hide()
            self.removeItemWidget(item)

    def mounted_items(self) -> tuple[QListWidgetItem, ...]:
        return tuple(item for item, _ in self._widgets.values())

    def mount_item(self, item: QListWidgetItem) -> QWidget:
        key = id(item)
        if key not in self._widgets:
            widget = self._make_widget(item)
            self._widgets[key] = item, widget
            self.setItemWidget(item, widget)
            widget.ensurePolished()
            self._fit_widget(item, widget)
        return self._widgets[key][1]

    def _make_widget(self, item: QListWidgetItem) -> QWidget:
        raise NotImplementedError

    def _fit_widget(self, item: QListWidgetItem, widget: QWidget) -> None:
        raise NotImplementedError

    def _top_item(self) -> QListWidgetItem | None:
        """The first visible item, even if the viewport starts in the spacing between cards."""
        width = self.viewport().width()
        for y in range(min(self.viewport().height(), 16)):
            if item := self.itemAt(QPoint(width // 2, y)):
                return item
        return None

    def _nearby(self, first: QListWidgetItem | None) -> list[QListWidgetItem]:
        if self.count() <= SMALL_LIST:
            return [self.item(n) for n in range(self.count()) if not self.item(n).isHidden()]
        if not self.isVisible():
            return []
        start = self.row(first) if first is not None else 0
        top, bottom = -BUFFER, self.viewport().height() + BUFFER
        nearby = []
        for n in range(start - 1, -1, -1):
            item = self.item(n)
            if item.isHidden():
                continue
            if self.visualItemRect(item).bottom() < top:
                break
            nearby.append(item)
        nearby.reverse()
        for n in range(start, self.count()):
            item = self.item(n)
            if item.isHidden():
                continue
            if self.visualItemRect(item).top() > bottom:
                break
            nearby.append(item)
        return nearby

    def refresh_widgets(self) -> None:
        self.doItemsLayout()
        first = self._top_item()
        before = self.visualItemRect(first).top() if first is not None else 0
        bar = self.verticalScrollBar()
        at_end = bar.value() > 0 and bar.value() == bar.maximum()
        nearby = self._nearby(first)
        keys = {id(item) for item in nearby}
        focused = QApplication.focusWidget()
        for key, (item, widget) in tuple(self._widgets.items()):
            if key not in keys and not (focused and (widget is focused or widget.isAncestorOf(focused))):
                self.forget_widget(item)
        for item in nearby:
            if mounted := self._widgets.get(id(item)):
                self._fit_widget(item, mounted[1])
            else:
                self.mount_item(item)
        self.doItemsLayout()
        if at_end:
            bar.setValue(bar.maximum())
        elif first is not None:
            bar.setValue(bar.value() + self.visualItemRect(first).top() - before)

    def _schedule_widgets(self) -> None:
        if self.isVisible():
            self._update_rows.start()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._schedule_widgets()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._schedule_widgets()
