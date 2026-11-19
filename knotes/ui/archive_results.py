# SPDX-License-Identifier: GPL-3.0-only
"""Search results in the meetings sidebar. Only the newest request is displayed."""

import dataclasses
import pathlib

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QListWidget, QListWidgetItem

from knotes import archive_search, catalog, failures, transcript
from knotes.language import tr
from knotes.ui.background import run_in_background


@dataclasses.dataclass
class Search:
    root: pathlib.Path
    meetings: list[catalog.Meeting]
    visible: list[catalog.Meeting]
    query: str
    project: str | None
    include_notes: bool


class ArchiveResults(QListWidget):
    opened = Signal(object)
    status = Signal(str)

    def __init__(self):
        super().__init__()
        self.index = archive_search.ArchiveIndex()
        self.wanted: Search | None = None
        self.running = False
        self.hits = None
        self.itemClicked.connect(self.open_item)
        self.hide()

    def open_item(self, item: QListWidgetItem | None) -> None:
        modifiers = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        if item and self.wanted and not QGuiApplication.keyboardModifiers() & modifiers:
            self.opened.emit(item.data(Qt.ItemDataRole.UserRole))

    def request(self, search: Search | None, refresh: bool = False) -> None:
        if self.wanted == search:
            if refresh and self.wanted and not self.running:
                self._start(self.wanted)
            return
        self.wanted = search
        self.hits = None
        self.clear()
        if search:
            self.status.emit(tr("Searching…"))
            if not self.running:
                self._start(search)

    def _start(self, search: Search) -> None:
        self.running = True

        def work() -> list[archive_search.Hit]:
            self.index.sync(search.root, search.meetings)
            return self.index.search(search.visible, search.query, search.project, search.include_notes)

        run_in_background(QThreadPool.globalInstance(), work, lambda result: self._ready(search, result))

    def _ready(self, search: Search, result: object) -> None:
        self.running = False
        if self.wanted is not search:
            if self.wanted:
                self._start(self.wanted)
            return
        if isinstance(result, Exception):
            self.hits = None
            self.status.emit(failures.text(result))
            return
        if result == self.hits:
            return
        self.hits = result
        self.clear()
        labels = {
            "meeting": tr("Meeting"),
            "transcript": tr("Transcript"),
            "minutes": tr("Minutes"),
            "notes": tr("Notes"),
            "notebook": tr("Notebook"),
        }
        for hit in result:
            caption = labels[hit.kind]
            if hit.second is not None:
                caption += " · " + transcript.time(hit.second)
            title = hit.title or tr("Untitled note")
            item = QListWidgetItem(f"{title} · {caption}" + ("\n" + hit.excerpt if hit.excerpt else ""), self)
            item.setData(Qt.ItemDataRole.UserRole, hit)
        self.status.emit("" if result else tr("Nothing found"))
