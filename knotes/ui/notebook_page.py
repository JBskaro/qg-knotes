# SPDX-License-Identifier: GPL-3.0-only
"""The notebook section in the main window."""

import dataclasses
import logging
import pathlib
from collections.abc import Callable

from PySide6.QtCore import QPoint, QSignalBlocker, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QCloseEvent,
    QDesktopServices,
    QFont,
    QKeySequence,
    QShortcut,
    QTextFormat,
)
from PySide6.QtWidgets import (
    QFrame,
    QInputDialog,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from knotes import catalog, failures, notebook, transcript
from knotes.language import tr, tr_key
from knotes.ui import style
from knotes.ui.notebook_editor import BOLD, DISC, ITALIC, MARKDOWN, NUMBER, STRIKE, NoteDocument, NoteEditor, link_url

log = logging.getLogger(__name__)
ROLE = Qt.ItemDataRole.UserRole
DOWN_ARROW = "⌄"
TABLE_ACTIONS = {
    "row+": tr_key("Insert row below"),
    "col+": tr_key("Insert column to the right"),
    "row-": tr_key("Delete row"),
    "col-": tr_key("Delete column"),
}


class NotebookPage(QWidget):
    def __init__(self, recordings: pathlib.Path, projects: list[str], project_looks: dict[str, dict] | None = None):
        super().__init__()
        self.recordings = recordings
        self.search_hint = tr("Search the notebook  Ctrl+F")
        self._query = ""
        self._projects = projects
        self._project_looks = project_looks or {}
        state = self._read_view()
        self._project_filter = state.project if state.project in ("", *projects) else None
        self._archived = state.archived
        self._current = notebook.Note()
        self._items: list[notebook.Note] = []
        self._timer = QTimer(self, singleShot=True, interval=500)
        self._timer.timeout.connect(self.save)
        self._build_list()
        self._build_editor()
        split = QSplitter()
        split.addWidget(self.list_panel)
        split.addWidget(self.editor_panel)
        split.setStretchFactor(1, 1)
        split.setSizes([220, 680])
        layout = QVBoxLayout(self)
        layout.addWidget(split)
        self._refresh()
        self._restore_view(state)

    def _read_view(self) -> notebook.View:
        try:
            return notebook.view(self.recordings)
        except OSError as error:
            log.warning("notebook view not read: %s", error)
            return notebook.View()

    def _restore_view(self, state: notebook.View) -> None:
        note = next((item for item in self._items if item.id == state.note_id and self._matches(item)), None)
        self._open(note or self._first())
        if note is not None:
            cursor = self.body.textCursor()
            cursor.setPosition(min(state.position, self.body.document().characterCount() - 1))
            self.body.setTextCursor(cursor)
            QTimer.singleShot(0, self.body, self.body.ensureCursorVisible)

    def _build_list(self) -> None:
        self.list_panel = QWidget()
        column = QVBoxLayout(self.list_panel)
        column.setContentsMargins(0, 0, 6, 0)
        new = QPushButton(tr("New note"))
        new.setObjectName("главная")
        new.clicked.connect(self.new_note)
        archive = QPushButton(tr("Архив"), checkable=True, checked=self._archived)
        archive.setObjectName("кнопка_блокнота")
        archive.clicked.connect(self._toggle_archive)
        self.archive = archive
        top = QHBoxLayout()
        top.addWidget(new, 1)
        top.addWidget(archive)
        self.list_ = QListWidget()
        self.list_.currentItemChanged.connect(self._choose)
        self.list_.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_.customContextMenuRequested.connect(self._note_menu)
        self.empty = style.caption("")
        self.project_button = QPushButton()
        self.project_button.setObjectName("кнопка_блокнота")
        self._show_project_filter()
        column.addLayout(top)
        column.addWidget(self.project_button)
        column.addWidget(self.list_, 1)
        column.addWidget(self.empty)
        QShortcut(QKeySequence("Ctrl+N"), self, self.new_note).setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)

    def _build_editor(self) -> None:
        self.editor_panel = QWidget()
        column = QVBoxLayout(self.editor_panel)
        column.setContentsMargins(12, 2, 6, 0)
        self.title = QLineEdit()
        self.title.setObjectName("название_встречи")
        self.title.setFrame(False)
        self.title.setPlaceholderText(tr("Note title"))
        self.title.setAccessibleName(tr("Note title"))
        font = self.title.font()
        font.setPointSizeF(font.pointSizeF() * 1.4)
        font.setBold(True)
        self.title.setFont(font)
        self.more = style.icon_button(style.MORE, tr("Note options"), self._options)
        self.more.setAccessibleName(tr("Note options"))
        heading = QHBoxLayout()
        heading.addWidget(self.title, 1)
        heading.addWidget(self.more)
        self.body = NoteEditor()
        self.body.setFrameShape(QFrame.Shape.NoFrame)
        self.body.setViewportMargins(6, 6, 6, 0)
        self.body.setPlaceholderText(tr("Write a note…"))
        self.body.setAccessibleName(tr("Note text"))
        QShortcut(QKeySequence("Ctrl+Return"), self.body, self._checklist)
        QShortcut(QKeySequence("Ctrl+Shift+H"), self.body, lambda: self._format(self.body.set_heading, 2))
        self.tools = self._toolbar()
        self.preview = QTextBrowser()
        self.preview.setDocument(NoteDocument(self.preview))
        self.preview.setFrameShape(QFrame.Shape.NoFrame)
        self.preview.setOpenLinks(False)
        self.preview.anchorClicked.connect(self._open_link)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.body)
        self.stack.addWidget(self.preview)
        self.status = style.caption("")
        self.status.setWordWrap(True)
        self.status.setObjectName("ошибка_текст")
        self.status.hide()
        column.addLayout(heading)
        column.addWidget(self.tools)
        column.addWidget(self.stack, 1)
        column.addWidget(self.status)
        self.title.returnPressed.connect(self.body.setFocus)
        self.title.textChanged.connect(lambda: self._timer.start())
        self.body.textChanged.connect(lambda: self._timer.start())
        self.body.cursorPositionChanged.connect(self._update_toolbar)
        self.body.currentCharFormatChanged.connect(self._update_toolbar)

    def _toggle_archive(self, archived: bool) -> None:
        self._set_filter(self._project_filter, archived)
        self.archive.setChecked(self._archived)

    def _show_project_filter(self) -> None:
        project = self._project_filter
        name = tr("All projects") if project is None else project or tr("No project")
        previous = self.project_button.menu()
        self.project_button.setText(style.menu_text(name))
        self.project_button.setAccessibleName(tr("Project filter"))
        self.project_button.setMenu(
            style.projects_menu(self.project_button, self._projects, self._project_looks, project, self._select_project)
        )
        if previous is not None:
            previous.deleteLater()

    def _select_project(self, project: str | None) -> None:
        self._set_filter(project, self._archived)
        self._show_project_filter()

    def _set_filter(self, project: str | None, archived: bool) -> None:
        if (project, archived) != (self._project_filter, self._archived) and self.save():
            self._project_filter, self._archived = project, archived
            self._show_list()
            if not self._matches(self._current):
                self._open(self._first())

    def _options(self) -> None:
        menu = QMenu(self)
        reading = bool(self.stack.currentIndex())
        menu.addAction(tr("Edit notes") if reading else tr("Preview note"), lambda: self._view(0 if reading else 1))
        menu.addSeparator()
        menu.addAction(tr("Unpin note") if self._current.pinned else tr("Pin note"), self._pin)
        projects = menu.addMenu(tr("Note project"))
        for project in ("", *self._projects):
            action = projects.addAction(project or tr("No project"))
            action.setCheckable(True)
            action.setChecked(project == self._current.project)
            action.triggered.connect(lambda _checked=False, name=project: self._set_project(name))
        menu.addAction(tr("Save as Markdown…"), self._export)
        if self._current.id:
            menu.addAction(tr("Restore from archive") if self._current.archived else tr("Archive note"), self._archive)
            menu.addSeparator()
            menu.addAction(tr("Delete note…"), lambda: self._delete(self._current))
        menu.exec(self.more.mapToGlobal(self.more.rect().bottomLeft()))

    def _note_menu(self, point: QPoint) -> None:
        item = self.list_.itemAt(point)
        if item is None:
            return
        note = item.data(ROLE)
        menu = QMenu(self.list_)
        menu.addAction(tr("Delete note…"), lambda: self._delete(note))
        menu.exec(self.list_.viewport().mapToGlobal(point))

    def _pin(self) -> None:
        if self.save() and self._current.id:
            self._store(dataclasses.replace(self._current, pinned=not self._current.pinned))

    def _set_project(self, project: str) -> None:
        if self.save() and self._current.id and project != self._current.project:
            self._store(dataclasses.replace(self._current, project=project))

    def _toolbar(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(4, 0, 4, 0)
        row.setSpacing(3)
        self.heading = self._menu_button(row, tr("Paragraph style"), tr("Text"))
        self.heading.setMinimumWidth(74)
        self.heading.menu().addAction(tr("Text"), lambda: self._format(self.body.set_heading, 0))
        for level in range(1, 7):
            label = tr("Heading {level}", level=level)
            self.heading.menu().addAction(label, lambda _checked=False, n=level: self._format(self.body.set_heading, n))
        lists = self._menu_button(row, tr("Lists"), icon="\ue8fd")
        lists.menu().addAction(tr("Bulleted list"), lambda: self._format(self.body.set_list, DISC))
        lists.menu().addAction(tr("Numbered list"), lambda: self._format(self.body.set_list, NUMBER))
        lists.menu().addAction(tr("Checklist items"), lambda: self._format(self.body.set_list, DISC, True))
        lists.menu().addAction(tr("Remove list"), lambda: self._format(self.body.set_list, None))
        self._styles = {
            BOLD: self._style_button(row, BOLD, "B", tr("Bold"), "Ctrl+B"),
            ITALIC: self._style_button(row, ITALIC, "I", tr("Italic"), "Ctrl+I"),
            STRIKE: self._style_button(row, STRIKE, "S", tr("Strikethrough"), "Ctrl+Shift+X"),
        }
        link = style.icon_button("\ue71b", tr("Insert link"), self._link)
        link.setAccessibleName(tr("Insert link"))
        row.addWidget(link)
        QShortcut(QKeySequence("Ctrl+K"), self.body, self._link)
        table = self._menu_button(row, tr("Table"), icon="\uf0e2")
        self._table_menu(table.menu())
        clear = style.icon_button("\ue75c", tr("Clear formatting"), lambda: self._format(self.body.clear_formatting))
        clear.setAccessibleName(tr("Clear formatting"))
        row.addWidget(clear)
        row.addStretch()
        return bar

    @staticmethod
    def _menu_button(row: QHBoxLayout, name: str, text: str = "", *, icon: str = "") -> QToolButton:
        button = QToolButton()
        button.setObjectName("буквы")
        button.setToolTip(name)
        if icon:
            button.setIcon(style.icon(icon, button.palette().buttonText().color()))
        button.setText(f"{text} {DOWN_ARROW}".strip())
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setProperty("formatMenu", True)
        button.setAccessibleName(name)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setMenu(QMenu(button))
        row.addWidget(button)
        return button

    def _style_button(
        self, row: QHBoxLayout, prop: QTextFormat.Property, label: str, name: str, shortcut: str
    ) -> QToolButton:
        button = QToolButton()
        button.setObjectName("буквы")
        button.setText(label)
        button.setAccessibleName(name)
        button.setToolTip(f"{name} ({shortcut})")
        button.setCheckable(True)
        font = QFont(button.font())
        font.setBold(prop == BOLD)
        font.setItalic(prop == ITALIC)
        font.setStrikeOut(prop == STRIKE)
        font.setPointSize(12)
        button.setFont(font)

        def apply() -> None:
            self._format(self.body.toggle_style, prop)

        button.clicked.connect(apply)
        QShortcut(QKeySequence(shortcut), self.body, apply)
        row.addWidget(button)
        return button

    def _format[**P](self, action: Callable[P, None], *args: P.args, **kwargs: P.kwargs) -> None:
        self._view(0)
        action(*args, **kwargs)
        self._update_toolbar()
        self.body.setFocus()

    def _update_toolbar(self) -> None:
        level = self.body.textCursor().blockFormat().headingLevel()
        label = f"H{level}" if level else tr("Text")
        self.heading.setText(f"{label} {DOWN_ARROW}")
        fmt = self.body.currentCharFormat()
        self._styles[BOLD].setChecked(fmt.fontWeight() >= QFont.Weight.Bold)
        for prop in (ITALIC, STRIKE):
            self._styles[prop].setChecked(bool(fmt.property(prop)))

    def _checklist(self) -> None:
        self._format(self.body.toggle_check)

    @staticmethod
    def _open_link(address: QUrl) -> None:
        if url := link_url(address.toString()):
            QDesktopServices.openUrl(url)

    def _link(self) -> None:
        fmt = self.body.currentCharFormat()
        address, accepted = QInputDialog.getText(self, tr("Insert link"), tr("Link address"), text=fmt.anchorHref())
        address = address.strip()
        if accepted and address:
            if url := link_url(address):
                self._format(self.body.set_link, url)
            else:
                QMessageBox.information(self, tr("Insert link"), tr("Enter an http, https or mailto address."))

    def _table_menu(self, menu: QMenu) -> None:
        insert = menu.addAction(tr("Insert table…"), self._insert_table)
        menu.addSeparator()
        actions = [
            menu.addAction(tr(label), lambda _checked=False, op=operation: self._format(self.body.edit_table, op))
            for operation, label in TABLE_ACTIONS.items()
        ]

        def update() -> None:
            table = self.body.textCursor().currentTable()
            insert.setEnabled(table is None)
            for operation, action in zip(TABLE_ACTIONS, actions, strict=True):
                allowed = table is not None and {"row-": table.rows(), "col-": table.columns()}.get(operation, 2) > 1
                action.setEnabled(allowed)

        menu.aboutToShow.connect(update)

    def _insert_table(self) -> None:
        rows, accepted = QInputDialog.getInt(self, tr("Insert table…"), tr("Rows"), 3, 1, 100)
        if accepted:
            columns, accepted = QInputDialog.getInt(self, tr("Insert table…"), tr("Columns"), 2, 1, 20)
            if accepted:
                self._format(self.body.insert_table, rows, columns)

    def _view(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.tools.setVisible(not index)
        if index:
            if self.body.document().isModified() or self._current.rich_text:
                self.preview.document().setHtml(self.body.document().toHtml())
            else:
                self.preview.document().setMarkdown(self._current.text, MARKDOWN)

    def _refresh(self) -> None:
        try:
            self._items = notebook.list_(self.recordings)
        except (OSError, ValueError) as error:
            self._report(error)
        self._show_list()

    def search(self, query: str) -> None:
        self._query = catalog.for_search(query.strip())
        self._show_list()

    def _matches(self, note: notebook.Note) -> bool:
        return (
            note.archived == self._archived
            and (self._project_filter is None or note.project == self._project_filter)
            and self._query in catalog.for_search(f"{note.title} {note.text}")
        )

    def _show_list(self) -> None:
        with QSignalBlocker(self.list_):
            self.list_.clear()
            for note in filter(self._matches, self._items):
                first = next((line for line in note.text.splitlines() if line.strip()), "")
                label = note.title or first[:70] or tr("Untitled note")
                item = QListWidgetItem(f"★  {label}" if note.pinned else label)
                item.setData(ROLE, note)
                item.setToolTip(note.project)
                self.list_.addItem(item)
                if note.id == self._current.id:
                    self.list_.setCurrentItem(item)
        empty = tr("No archived notes") if self._archived else tr("No notes here yet")
        self.empty.setText(tr("No matching notes") if self._query else empty)
        self.empty.setVisible(not self.list_.count())

    def _first(self) -> notebook.Note:
        item = self.list_.item(0)
        return item.data(ROLE) if item is not None else notebook.Note(project=self._project_filter or "")

    def _choose(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        note = item.data(ROLE)
        if note.id == self._current.id:
            return
        if self.save():
            self._open(note)
        else:
            self._show_list()

    def _open(self, note: notebook.Note) -> None:
        self._current = note
        self.title.setText(note.title)
        self.body.load_text(note.text, note.rich_text)
        self._update_toolbar()
        self._timer.stop()
        self._view(0)
        self.status.hide()
        self._show_list()

    def new_note(self) -> None:
        if self.save():
            self._archived = False
            self.archive.setChecked(False)
            self._open(notebook.Note(project=self._project_filter or ""))
            self.body.setFocus()

    def save(self) -> bool:
        self._timer.stop()
        document = self.body.document()
        note = dataclasses.replace(
            self._current,
            title=self.title.text().strip(),
            text=self.body.source() if document.isModified() else self._current.text,
            rich_text=document.toHtml() if document.isModified() else self._current.rich_text,
        )
        if note == self._current or not (note.id or note.title or note.text.strip()):
            return True
        return self._store(note)

    def _store(self, note: notebook.Note) -> bool:
        try:
            self._current = notebook.save(self.recordings, note, self._current)
        except (OSError, ValueError) as error:
            self._report(error)
            return False
        self.body.document().setModified(False)
        self.status.hide()
        self._refresh()
        return True

    def _report(self, error: OSError | ValueError) -> None:
        log.warning("notebook: %s", type(error).__name__)
        self.status.setText(tr("Notes not saved: {reason}", reason=failures.text(error)))
        self.status.show()

    def _archive(self) -> None:
        if self.save() and self._store(dataclasses.replace(self._current, archived=not self._current.archived)):
            self._open(self._first())

    def _delete(self, note: notebook.Note) -> None:
        active = note.id == self._current.id
        name = (self.title.text().strip() if active else note.title) or tr("Untitled note")
        heading = tr("Delete note…")
        if not style.ask(self, heading, tr('Permanently delete the note "{name}"?', name=name), warning=True):
            return
        try:
            notebook.delete(self.recordings, self._current if active else note)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, heading, failures.text(error))
            return
        self._items = [item for item in self._items if item.id != note.id]
        self._show_list()
        if active:
            self._open(self._first())

    def _export(self) -> None:
        if not self.save() or not self._current.id:
            return
        filename, _ = QFileDialog.getSaveFileName(self, tr("Save as Markdown…"), "note.md", "Markdown (*.md)")
        if not filename:
            return
        try:
            title = f"# {self._current.title}\n\n" if self._current.title else ""
            transcript.write_text(pathlib.Path(filename), title + self._current.text)
        except OSError as error:
            self._report(error)

    def can_close(self) -> bool:
        return self.save()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.save():
            event.ignore()
            return
        state = notebook.View(self._project_filter, self._current.id, self.body.textCursor().position(), self._archived)
        try:
            notebook.remember_view(self.recordings, state)
        except OSError as error:
            log.warning("notebook view not saved: %s", error)
        super().closeEvent(event)
