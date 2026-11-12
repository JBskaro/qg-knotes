"""A scroll area that keeps only nearby groups of transcript rows on screen."""

from bisect import bisect_right
from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, QTimer, Qt
from PySide6.QtWidgets import QFrame, QScrollArea, QWidget

TOP, LEFT, RIGHT = 4, 4, 12
BUFFER = 160  # keep a little text ready in each scroll direction


class TranscriptScroll(QScrollArea):
    def __init__(self, create: Callable[[int], QWidget], keep: Callable[[QWidget], bool]):
        super().__init__()
        self._create, self._keep = create, keep
        self._heights: list[int] = []
        self._offsets = [TOP]
        self.mounted: dict[int, QWidget] = {}
        self.canvas = QWidget()
        self.setWidget(self.canvas)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._pending = QTimer(self, singleShot=True, interval=0)
        self._pending.timeout.connect(self._update_pages)
        self.verticalScrollBar().valueChanged.connect(self._schedule)
        self.viewport().installEventFilter(self)

    def set_pages(self, estimates: list[int], position: int) -> None:
        for index in tuple(self.mounted):
            self._remove(index)
        self._heights = estimates
        self._positions()
        self.verticalScrollBar().setValue(position)
        self._update_pages()

    def _positions(self) -> None:
        offsets = [TOP]
        for height in self._heights:
            offsets.append(offsets[-1] + height)
        self._offsets = offsets
        self.canvas.setMinimumHeight(offsets[-1] + TOP)

    def _page_at(self, y: int) -> int:
        return max(0, min(len(self._heights) - 1, bisect_right(self._offsets, y) - 1))

    def _mount(self, index: int) -> QWidget:
        if index not in self.mounted:
            page = self._create(index)
            page.setParent(self.canvas)
            page.installEventFilter(self)
            self.mounted[index] = page
        return self.mounted[index]

    def _remove(self, index: int) -> None:
        page = self.mounted.pop(index)
        page.removeEventFilter(self)
        page.hide()
        page.deleteLater()

    def _arrange(self, follow_bottom: bool = False) -> None:
        bar = self.verticalScrollBar()
        y = bar.value()
        at_bottom = follow_bottom and 0 < y == bar.maximum()
        anchor = self._page_at(y)
        within = y - self._offsets[anchor]
        width = max(1, self.viewport().width() - LEFT - RIGHT)
        for index, page in self.mounted.items():
            page.ensurePolished()
            layout = page.layout()
            height = layout.totalHeightForWidth(width) if layout.hasHeightForWidth() else layout.sizeHint().height()
            self._heights[index] = max(1, height)
        self._positions()
        for index, page in self.mounted.items():
            page.setGeometry(LEFT, self._offsets[index], width, self._heights[index])
            if page.isHidden():
                page.show()
            page.layout().activate()
        # When an estimated page above the viewport gets measured, keep the same page in view.
        bar.setValue(bar.maximum() if at_bottom else self._offsets[anchor] + min(within, self._heights[anchor] - 1))

    def _update_pages(self) -> None:
        if not self._heights:
            return
        y = self.verticalScrollBar().value()
        first = self._page_at(y - BUFFER)
        last = self._page_at(y + self.viewport().height() + BUFFER)
        for index in tuple(self.mounted):
            if not first <= index <= last and not self._keep(self.mounted[index]):
                self._remove(index)
        for index in range(first, last + 1):
            self._mount(index)
        self._arrange(follow_bottom=True)
        # A page may turn out shorter than estimated and expose part of the next page.
        if self._page_at(self.verticalScrollBar().value() + self.viewport().height()) > last:
            self._schedule()

    def page(self, index: int) -> QWidget:
        """Materialize a distant page before a time, mark or search jump."""
        page = self._mount(index)
        self._arrange()
        return page

    def reveal(self, index: int, row: QWidget) -> None:
        y = row.mapTo(self.canvas, row.rect().topLeft()).y()
        # Start inside the target page so that measuring a previous page cannot move the target out of view.
        self.verticalScrollBar().setValue(max(self._offsets[index], y - 100))

    def _schedule(self) -> None:
        if self.isVisible():
            self._pending.start()

    def showEvent(self, event: QEvent) -> None:
        super().showEvent(event)
        self._schedule()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.Resize, QEvent.Type.LayoutRequest):
            self._schedule()
        return super().eventFilter(watched, event)
