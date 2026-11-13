# SPDX-License-Identifier: GPL-3.0-only
"""Window with the meeting list: sections and projects on the left, like folders in email; meetings by day on the
right, each with its state and main action right in the row. A meeting can be dragged onto a project on the left.
Tasks, summary, settings and help open to the right of the sections column, in place of the list and the meeting.

Anything can be sent: an own recording not sent before, a ready one (again), a video or audio file - by dragging,
through "choose a file" or by putting it into the recordings folder.
"""

import dataclasses
import pathlib

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDragEnterEvent,
    QDropEvent,
    QGuiApplication,
    QKeySequence,
    QPalette,
    QResizeEvent,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from knotes import disk_names
from knotes import catalog, transcript, system, meeting_list, meeting_rows, project_rename
from knotes.meeting_rows import MeetingItem, Progress
from knotes.ui import tour
from knotes.ui import style
from knotes.ui.meeting_row import MeetingRow
from knotes.ui.meetings_list import DAY_ROLE, MeetingsList
from knotes.ui.page_motion import fade_in
from knotes.ui.project_page import ProjectPage
from knotes.ui.projects_panel import ProjectsPanel, looks_menu, minutes_menu
from knotes.ui.sidebar import STRIP_WIDTH, SidebarPanel
from knotes.language import tr_key, tr

RECORDING_FILES = tr_key("Записи встреч (*.mp4 *.mkv *.webm *.mov *.avi *.m4a *.mp3 *.wav *.ogg *.flac);;Все файлы (*)")
LEFT = "\ue76b"  # icon font arrow: hide the meeting list
ALL_HIDDEN = frozenset({"проекты", "встречи"})  # reading at full width: sections as a strip, no list
LIST_MIN_WIDTH = 250  # meeting titles stay readable
MEETING_WIDTH = 560  # a comfortable width of the open meeting: below it the sections fold to icons


class MeetingsWindow(QWidget):
    start_recording = Signal()
    update_requested = Signal()
    transcribe = Signal(list)  # list[pathlib.Path] - recording files
    recognize = Signal(list)  # list[pathlib.Path] - meetings from the list (folders or files)
    open_meeting = Signal(pathlib.Path)
    config = Signal()
    help_page = Signal()
    summary = Signal()
    tasks = Signal(object)  # task board: project (None - all)
    open_notes = Signal(pathlib.Path)  # a meeting on the "Notes" tab, from the project page feed
    changed = Signal()  # renamed, project set, deleted: reread the list
    projects_changed = Signal(list)  # created projects: save to the settings
    project_looks_changed = Signal(dict)  # project colors and icons: save to the settings
    layout_changed = Signal(dict)  # hidden columns and widths: save to the settings
    in_sight = Signal()  # the list is in sight again after a section page or a minimized window: reread it
    notice = Signal(str, str)  # heading and text of a message to show as a toast

    def __init__(self):
        super().__init__()
        self.setWindowTitle(style.NAME)
        self.setObjectName("основа")
        self.setWindowIcon(style.tray_icon(False))
        self.setAcceptDrops(True)
        self.resize(1320, 760)
        self._hidden: set[str] = set()  # "проекты" - sections column collapsed, "встречи" - list hidden
        self._before_expand: set[str] = set()  # what was hidden before reading at full width
        self._auto_folded = False  # the window got too narrow and folded the sections, not the user
        self._narrow = False
        self._heading = style.NAME  # window title with the open meeting
        self._kinds: list[MeetingItem] = []
        self._snapshot: tuple = ()  # what the list shows: query, project, rows
        self._found: tuple = ()  # search matches: query, meetings searched, matches
        self.project: str | None = None  # selected on the left: None - all meetings, "" - no project
        self.recordings_folder: pathlib.Path | None = None
        self._known: list[str] = []
        self._project_looks: dict[str, dict] = {}  # project -> color and icon
        self.recording = QPushButton()
        self.recording.clicked.connect(self.start_recording)
        self.set_recording(False)
        self._build_search_bar()
        self._meetings_query = ""  # kept while a section with its own search (the help) has the search box
        self.list_ = MeetingsList(self._row_widget)
        self.list_.setObjectName("встречи")
        self.list_.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # a row fits the column
        self.list_.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_.itemClicked.connect(self._clicked_item)
        self.list_.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_.customContextMenuRequested.connect(
            lambda point: self._menu(self.list_.viewport().mapToGlobal(point))
        )
        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setObjectName("второй_текст")
        self.panel = ProjectsPanel()
        self.panel.selected.connect(self._pick_project)
        self.panel.assign.connect(self._assign)
        self.panel.menu.connect(self._project_menu)
        self._build()
        self._shortcuts()
        self.repaint_theme()

    def _build_search_bar(self) -> None:
        self.update_button = QPushButton(tr("Update"))
        self.update_button.hide()
        self.update_button.clicked.connect(self.update_requested)
        self.search = style.ClearableLine()
        self.search.setPlaceholderText(tr("Search all meetings  Ctrl+F"))
        self.search.setClearButtonEnabled(True)
        self.search.addAction(style.icon(style.SEARCH, "#888780"), QLineEdit.ActionPosition.LeadingPosition)
        self._search_delay = QTimer(self, singleShot=True, interval=300)
        self._search_delay.timeout.connect(lambda: self._show_items(always=True))
        self.search.textChanged.connect(self._search_changed)

    def _build(self) -> None:
        """Recording and search on top; below, three columns on one splitter: sections with projects, meetings and
        the open meeting. A section (tasks, summary, settings, help) opens in place of the list and the meeting. The
        sections column can be collapsed to a strip, the meeting list hidden, the borders move with the mouse."""
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(self.recording)
        top.addWidget(self.search, 1)
        top.addWidget(self.update_button)
        self.projects_column = SidebarPanel(self.panel)
        self.projects_column.selected.connect(self._section_chosen)
        self.projects_column.new_project.connect(self._new_project)
        self.meetings_column = self._column(
            tr("Встречи"), "встречи", tr("Hide the meeting list  Ctrl+2"), self.list_, self.empty, self._drop_area()
        )
        self.meetings_column.setObjectName("слой")  # sections on the window background, list and meeting as cards
        self.meetings_column.setMinimumWidth(LIST_MIN_WIDTH)
        placeholder = QLabel(tr("Select a meeting in the list"))
        placeholder.setObjectName("тихий_текст")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.details = QStackedWidget()
        self.details.addWidget(placeholder)
        self.list_button = QPushButton(
            style.icon(style.EXPAND_PANEL, self.palette().color(QPalette.ColorRole.ButtonText)),
            "  " + tr("Meeting list"),
        )
        self.list_button.setToolTip(tr("Show the meeting list  Ctrl+2"))
        self.list_button.clicked.connect(lambda: self.show_column("встречи", True))
        self._list_row = QWidget()  # where the hidden list was: the way back is in sight, not only Ctrl+2
        list_row = QHBoxLayout(self._list_row)
        list_row.setContentsMargins(0, 0, 0, 0)
        list_row.addWidget(self.list_button)
        list_row.addStretch()
        self._list_row.hide()
        self._details_layer = style.layer(self._list_row, self.details, padding=4)
        self.right_ = QStackedWidget()  # the open meeting or a section page
        self.right_.addWidget(self._details_layer)
        self.columns = QSplitter(Qt.Orientation.Horizontal)
        self.columns.setObjectName("колонки")
        self.columns.setHandleWidth(8)  # the window background shows between the cards
        self.columns.setChildrenCollapsible(False)
        for widget in (self.projects_column, self.meetings_column, self.right_):
            self.columns.addWidget(widget)
        self.columns.setStretchFactor(2, 1)
        self._widths = [210, 380, 760]  # last column widths; a hidden or collapsed column keeps its previous one
        self.columns.setSizes(self._widths)
        self.columns.splitterMoved.connect(lambda *_: self._remember.start())
        self._remember = QTimer(self, singleShot=True, interval=600)  # layout is saved after the mouse is released
        self._remember.timeout.connect(lambda: self.layout_changed.emit(self.window_layout()))
        self.page: QWidget | None = None  # section on the right in place of the list and the meeting
        self.meeting: QWidget | None = None  # the open meeting to the right of the list
        self.project_page: ProjectPage | None = None  # on the right when a project is selected and no meeting is open
        column = QVBoxLayout(self)
        column.addLayout(top)
        column.addWidget(self.columns, 1)

    def _column(self, heading: str, name: str, tooltip: str, *widgets: QWidget) -> QWidget:
        """A column with the header "Heading ‹": "‹" hides the column."""
        header = QHBoxLayout()
        header.setContentsMargins(4, 0, 0, 0)
        caption = QLabel(heading)
        caption.setObjectName("второй_текст")
        header.addWidget(caption)
        header.addStretch()
        header.addWidget(style.icon_button(LEFT, tooltip, lambda: self.show_column(name, False)))
        col = QFrame()  # QFrame: the column can be a card ("слой")
        column = QVBoxLayout(col)
        column.setContentsMargins(6, 6, 6, 6)
        column.setSpacing(4)
        column.addLayout(header)
        for widget in widgets:
            column.addWidget(widget, 1 if isinstance(widget, QListWidget) else 0)
        return col

    def _drop_area(self) -> QFrame:
        """One short line under the list, wrapped only in a narrow column; the formats are in the tooltip."""
        drop = QFrame()
        drop.setObjectName("бросить")
        icon = f'<span style=\'font-family: "Segoe Fluent Icons", "Segoe MDL2 Assets";\'>{style.UPLOAD}</span>&nbsp; '
        text = QLabel(icon + tr("Drop a recording or <a href='file'>choose a file</a>"), drop)
        text.setObjectName("второй_текст")
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text.setToolTip(tr("Video or audio of a meeting: mp4, mkv, webm, m4a, mp3 and others. The app transcribes it."))
        text.linkActivated.connect(self.pick_files)
        QHBoxLayout(drop).addWidget(text)
        return drop

    def _shortcuts(self) -> None:
        """List keys work only in the meetings column: in the meeting open next to it Delete must not delete it."""
        for key_seq, action in (
            (QKeySequence.StandardKey.Find, self.search.setFocus),
            ("Return", self._open_item),
            ("Enter", self._open_item),
            ("F2", self._rename),
            (QKeySequence.StandardKey.Delete, self._delete),
        ):
            QShortcut(QKeySequence(key_seq), self.meetings_column, action).setContext(
                Qt.ShortcutContext.WidgetWithChildrenShortcut
            )
        for key_seq, action in (
            ("Alt+Left", self.go_back),
            ("F11", self.expand),
            ("Ctrl+1", lambda: self.show_column("проекты", None)),
            ("Ctrl+2", lambda: self.show_column("встречи", None)),
            ("Ctrl+3", self._switch_tab),
        ):
            QShortcut(QKeySequence(key_seq), self, action)

    def repaint_theme(self) -> None:
        self.setStyleSheet(style.window_style())
        self._snapshot = ()
        self.panel.refresh_view()
        self.projects_column.repaint_theme()
        if self._kinds:
            self._show_items()
        for page in (self.page, self.meeting):
            if page is not None and hasattr(page, "repaint_theme"):
                page.repaint_theme()

    # --- sections: pages in place of the list and the meeting ---------------------------------------------

    def _section_chosen(self, section: str) -> None:
        """A click on a section on the left. "Meetings" goes to the list and brings it back if it is hidden, also from
        another section; the program opens the other sections (signals) and calls show_page."""
        if section == "свернуть":
            self.show_column("проекты", None)
        elif section == "встречи":
            if self.page is not None:
                self.go_back()
            if "встречи" in self._hidden:
                self.restore_columns()
        else:
            {
                "задачи": lambda: self.tasks.emit(self.project),
                "сводка": self.summary.emit,
                "настройки": self.config.emit,
                "справка": self.help_page.emit,
            }[section]()

    def show_page(self, widget: QWidget, heading: str, section: str = "") -> None:
        """A section to the right of the sections column, in place of the list and the meeting; the previous section
        page closes."""
        self._close_page()
        self.page = widget
        if hasattr(widget, "search_hint"):
            self._lend_search(widget)
        self.right_.addWidget(widget)
        self.right_.setCurrentWidget(widget)
        self.projects_column.select(section)
        self._apply_columns()
        self.setWindowTitle(f"{heading} - {style.NAME}")
        widget.setFocus()
        fade_in(widget)

    def go_back(self) -> None:
        """Back to the meetings: the section page closes, the list and the meeting are on the right again."""
        covered = self.page is not None
        self._close_page()
        self.projects_column.select("встречи")
        self._apply_columns()
        self.setWindowTitle(self._heading)
        if covered:
            self.in_sight.emit()
            fade_in(self._details_layer)

    def list_in_sight(self) -> bool:
        """The list is worth rereading: the window is open, not minimized, no section page covers the list."""
        return self.isVisible() and not self.isMinimized() and self.page is None

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and self.list_in_sight():
            self.in_sight.emit()

    def _close_page(self) -> None:
        """The page closes like a window: a meeting stops playback and saves the minutes edit."""
        if self.page is None:
            return
        widget, self.page = self.page, None
        if hasattr(widget, "search_hint"):
            self._take_search_back()
        self.right_.setCurrentWidget(self._details_layer)
        widget.close()
        self.right_.removeWidget(widget)
        widget.deleteLater()

    # --- open meeting and columns --------------------------------------------------------------------------

    def show_meeting(self, widget: QWidget, heading: str) -> bool:
        """The meeting goes to the right of the list; the previously open one closes (saving the minutes edit), so does
        an open section: the meeting was opened to be looked at. False: the open one kept an edit it could not save,
        it stays open."""
        if not self.close_meeting():
            return False
        self._close_page()
        self.projects_column.select("встречи")
        self.meeting = widget
        self.details.addWidget(widget)
        self.details.setCurrentWidget(widget)
        if hasattr(widget, "expand"):
            widget.expand.connect(self.expand)
        self._apply_columns()
        self._heading = f"{heading} - {style.NAME}"
        self.setWindowTitle(self._heading)
        self._highlight(getattr(widget, "folder", None))
        fade_in(widget)
        return True

    def close_meeting(self) -> bool:
        """False: the meeting stays open, it has an edit that could not be saved and the user keeps it."""
        if self.meeting is None:
            return True
        if not self.meeting.close():
            return False
        widget, self.meeting = self.meeting, None
        if self.project_page is not None:
            self.details.setCurrentWidget(self.project_page)
        else:
            self.details.setCurrentIndex(0)
        self.details.removeWidget(widget)
        self._heading = style.NAME
        self.setWindowTitle(style.NAME)
        return True

    def _highlight(self, folder: pathlib.Path | None) -> None:
        """The open meeting is selected in the list too, even when it was opened from a message."""
        for number in range(self.list_.count()):
            element = self.list_.item(number)
            kind = element.data(Qt.ItemDataRole.UserRole)
            if kind and kind.meeting.folder == folder:
                self.list_.setCurrentItem(element)
                return

    def _switch_tab(self) -> None:
        if self.meeting is not None and hasattr(self.meeting, "switch_tab"):
            self.meeting.switch_tab()

    def show_column(self, name: str, on: bool | None) -> None:
        """Name "проекты" expands or collapses the sections column to a strip, "встречи" shows or hides the list;
        None toggles."""
        hide_ = name not in self._hidden if on is None else not on
        self._hidden = self._hidden | {name} if hide_ else self._hidden - {name}
        if name == "проекты":
            self._auto_folded = False
        self._apply_columns()
        self.layout_changed.emit(self.window_layout())

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Too narrow for three columns at a readable width: the sections fold to icons by themselves, as the
        navigation pane of Windows apps does, and unfold when the window is wide again. Only crossing the width
        acts: a column the user folded or unfolded by hand stays as it is."""
        super().resizeEvent(event)
        narrow = self.width() < self._widths[0] + LIST_MIN_WIDTH + MEETING_WIDTH + 40
        if narrow == self._narrow:
            return
        self._narrow = narrow
        if narrow and "проекты" not in self._hidden:
            self._auto_folded, self._hidden = True, self._hidden | {"проекты"}
            self._apply_columns()
        elif not narrow and self._auto_folded:
            self._auto_folded, self._hidden = False, self._hidden - {"проекты"}
            self._apply_columns()

    def _apply_columns(self) -> None:
        """Columns by what is hidden and whether a section is open; an expanded sections column gets its previous
        width back. The icon in the meeting shows whether it brings the columns back."""
        sections, collapsed = self.projects_column, "проекты" in self._hidden
        if sections.collapsed != collapsed:  # QSplitter neither gives away the freed space nor restores the width
            widths = self.columns.sizes()
            if collapsed:
                self._widths[0] = widths[0] or self._widths[0]
            sections.collapse(collapsed)
            new_one = STRIP_WIDTH if collapsed else self._widths[0]
            widths[2] = max(0, widths[2] + widths[0] - new_one)
            widths[0] = new_one
            self.columns.setSizes(widths)
        self.meetings_column.setVisible("встречи" not in self._hidden and self.page is None)
        self._list_row.setVisible("встречи" in self._hidden and self._hidden != ALL_HIDDEN and self.page is None)
        if self.meeting is not None and hasattr(self.meeting, "full_width"):
            self.meeting.full_width(self._hidden == ALL_HIDDEN)

    def restore_columns(self) -> None:
        self._hidden = set()
        self._apply_columns()
        self.layout_changed.emit(self.window_layout())

    def expand(self) -> None:
        """Read the meeting at full width: sections column to a strip, list hidden; again - back as it was."""
        if self._hidden != ALL_HIDDEN:
            self._before_expand, self._hidden = set(self._hidden), set(ALL_HIDDEN)
        else:
            self._hidden = set() if self._before_expand == ALL_HIDDEN else set(self._before_expand)
        self._apply_columns()
        self.layout_changed.emit(self.window_layout())

    def window_layout(self) -> dict:
        """What to remember until the next start: collapsed and hidden columns and widths (a collapsed column keeps
        its previous one)."""
        widths = self.columns.sizes()
        if self.projects_column.collapsed:
            widths[0] = 0
        self._widths = [new_one or previous_one for new_one, previous_one in zip(widths, self._widths, strict=True)]
        hidden = self._hidden - ({"проекты"} if self._auto_folded else set())
        return {"скрыто": [name for name in ("проекты", "встречи") if name in hidden], "ширины": self._widths}

    def set_layout(self, window_layout: dict) -> None:
        if len(window_layout.get("ширины") or []) == len(self._widths):
            self._widths = [int(w_) for w_ in window_layout["ширины"]]
            self.columns.setSizes(self._widths)
        self._hidden = set(window_layout.get("скрыто", [])) & ALL_HIDDEN
        self._auto_folded = self._narrow = False
        self._apply_columns()

    def closeEvent(self, event: QCloseEvent) -> None:
        """The window hides by the clock: the page and the meeting close (the minutes edit is saved, sound stops). A
        meeting that keeps an edit it could not save keeps the window open too."""
        self.go_back()
        if not self.close_meeting():
            event.ignore()
            return
        super().closeEvent(event)

    def show_meetings(
        self,
        meetings: list[catalog.Meeting],
        busy_items: dict[pathlib.Path, meeting_list.Busy],
        progress: dict[pathlib.Path, Progress],
        price_per_second: float | None,
    ) -> None:
        """Meeting list; for busy ones, what the program is doing with them now ("Recording", "Transcribing")."""
        self._kinds = meeting_rows.items(meetings, busy_items, progress, price_per_second)
        self.projects_column.set_counts(meetings=len(self._kinds))
        self._show_projects()
        self._show_items()

    def show_progress(self, folder: pathlib.Path, progress: Progress) -> None:
        """Recognition progress of one meeting: only its row is rebuilt, the rest of the list stays as it is."""
        self._kinds = meeting_rows.with_progress(self._kinds, folder, progress)
        if not self._snapshot:
            return
        query, project, rows = self._snapshot
        self._snapshot = (
            query,
            project,
            tuple(
                (dataclasses.replace(v, progress=progress), e) if v.meeting.folder == folder else (v, e)
                for v, e in rows
            ),
        )
        for number in range(self.list_.count()):
            element = self.list_.item(number)
            kind = element.data(Qt.ItemDataRole.UserRole)
            if kind and kind.meeting.folder == folder:
                self._row(element, dataclasses.replace(kind, progress=progress))
                self.list_.refresh_widgets()
                break

    def set_tasks(self, open_items: int) -> None:
        """How many open tasks are on the board: the number by the "Tasks" section."""
        self.projects_column.set_counts(tasks=open_items)

    def set_projects(self, known: list[str], kind: dict[str, dict] | None = None) -> None:
        """Created projects from the settings (shown on the left even with no meetings yet), their colors and icons."""
        self._known = list(known)
        self._project_looks = dict(kind or {})

    def _projects(self) -> list[str]:
        return catalog.projects([v.meeting for v in self._kinds], self._known)

    def _show_projects(self) -> None:
        projects = self._projects()
        if self.project and self.project not in projects:
            self.project = None
        self.panel.display(
            catalog.account_by_projects([v.meeting for v in self._kinds]), projects, self.project, self._project_looks
        )

    def _pick_project(self, project: str | None) -> None:
        """A project filters the meetings; on the board, the tasks too (the board reopens for it); from other
        sections a click on a project leads to its meetings. Its page replaces an open meeting: the meeting is one
        click away in the list."""
        self.project = project
        if self.page is not None:
            if self.projects_column.section == "задачи":
                self.tasks.emit(project)
            else:
                self.go_back()
        if project and self.page is None and self.meeting is not None:
            self.close_meeting()
            self.list_.clearSelection()
        if not hasattr(self.page, "search_hint"):
            self.search.setPlaceholderText(self._meetings_hint())
        self._show_items(always=True)
        self._show_project_page(project)
        if self.page is None and self.meeting is None:
            fade_in(self.details.currentWidget())

    def _meetings_hint(self) -> str:
        return (
            tr("Search all meetings  Ctrl+F")
            if self.project is None
            else tr("Search meetings without a project  Ctrl+F")
            if self.project == ""
            else tr("Search in {project}  Ctrl+F", project=self.project)
        )

    def _search_changed(self, text: str) -> None:
        """The search box serves the open section if it can search (the help), otherwise the meeting list."""
        if hasattr(self.page, "search_hint"):
            self.page.search(text)
        else:
            self._search_delay.start()

    def _lend_search(self, page: QWidget) -> None:
        """A section with its own search takes the search box; the meeting query comes back when it closes."""
        self._meetings_query = self.search.text()
        if self._search_delay.isActive():  # the meeting query typed just before still applies
            self._search_delay.stop()
            self._show_items(always=True)
        self.search.clear()  # with its signal, or the clear button stays on the empty box; the help shows its topics
        self.search.setPlaceholderText(page.search_hint)
        if hasattr(page, "search_hint_changed"):  # the board's own project button changes what it searches
            page.search_hint_changed.connect(self.search.setPlaceholderText)
        QShortcut(QKeySequence(QKeySequence.StandardKey.Find), page, self.search.setFocus).setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )

    def _take_search_back(self) -> None:
        self.search.setText(self._meetings_query)
        self._search_delay.stop()  # the list kept this query all along: nothing to search again
        self.search.setPlaceholderText(self._meetings_hint())

    def _show_project_page(self, project: str | None) -> None:
        """With a project selected its page is on the right: meeting preparation, project info, meeting notes. A
        rebuild keeps an open meeting on top; a click on the project closes it first (_pick_project)."""
        self._remove_project_page(save=True)
        if not project or self.recordings_folder is None:
            if self.meeting is None:
                self.details.setCurrentIndex(0)
            return
        page = ProjectPage(self.recordings_folder, project)
        page.open_notes.connect(self.open_notes)
        page.board.connect(self.tasks.emit)
        self.details.addWidget(page)
        self.project_page = page
        if self.meeting is None:
            self.details.setCurrentWidget(page)

    def _remove_project_page(self, save: bool) -> None:
        page, self.project_page = self.project_page, None
        if page is None:
            return
        if save:
            page.close()  # closing saves what was edited on it
        else:
            page.close_without_saving()
        self.details.removeWidget(page)
        page.deleteLater()

    def refresh_project_page(self) -> None:
        """After a meeting the project has new notes and agenda: rebuild the page unless someone is typing on it."""
        page = self.project_page
        if page is not None and not page.editing():
            self._show_project_page(page.project)

    def _assign(self, folders: list[str], project: str) -> None:
        catalog.to_project([pathlib.Path(p) for p in folders], project)
        self.changed.emit()

    def _save_projects(self, projects: list[str]) -> None:
        self._known = sorted(set(projects), key=str.lower)
        self.projects_changed.emit(self._known)

    def _new_project(self) -> None:
        name, yes = QInputDialog.getText(self, tr("New project"), tr("Project name:"))
        name = name.strip()
        if yes and name:
            self._save_projects([*self._known, name])
            self.project = name
            self._show_projects()
            self._pick_project(name)

    def _project_menu(self, project: str, point: QPoint) -> None:
        menu = QMenu(self)
        menu.addAction(tr("New project…"), self._new_project)
        if project:
            menu.addAction(tr("Project board"), lambda: self.tasks.emit(project))
            menu.addAction(tr("Rename project…"), lambda: self._rename_project(project))
            looks_menu(
                menu,
                self._project_looks.get(project, {}),
                self.palette().color(QPalette.ColorRole.WindowText),
                lambda **fields: self._set_project_kind(project, **fields),
            )
            if self.recordings_folder is not None:
                minutes_menu(menu, project, self.recordings_folder)
            menu.addAction(tr("Delete project…"), lambda: self._delete_project(project))
        menu.exec(point)

    def _set_project_kind(self, project: str, **fields: object) -> None:
        """Project color or icon; no color and the folder icon are the defaults, such fields are not stored."""
        kind = {
            k: z
            for k, z in (self._project_looks.get(project, {}) | disk_names.to_disk(fields)).items()
            if z is not None and not (k == "значок" and z == "папка")
        }
        self._save_kind(
            {**{p: v for p, v in self._project_looks.items() if p != project}, **({project: kind} if kind else {})}
        )

    def _save_kind(self, kind: dict[str, dict]) -> None:
        self._project_looks = kind
        self.project_looks_changed.emit(dict(kind))
        self.panel.refresh_view()
        self._show_projects()
        self._show_items(always=True)

    def _rename_project(self, old_value: str) -> None:
        new_value, yes = QInputDialog.getText(self, tr("Rename project"), tr("Project name:"), text=old_value)
        new_value = new_value.strip()
        if not yes or not new_value or new_value == old_value or not self._move_project(old_value, new_value):
            return
        self._save_projects([new_value if p == old_value else p for p in self._projects()])
        if self.project == old_value:
            self.project = new_value
        if old_value in self._project_looks:  # color and icon move to the new name
            self._save_kind({new_value if p == old_value else p: v for p, v in self._project_looks.items()})
        self.changed.emit()

    def _move_project(self, old_value: str, new_value: str) -> bool:
        """Meetings, notes and own tasks follow the project, all of them or none; an empty new name means the
        project is deleted: no notes, meetings and tasks without a project. An open project page is saved under the
        old name before the move and closed: otherwise on closing it would bring the old name back. False: the
        project kept its name, the user is told why."""
        page = self.project_page
        if page is not None and page.project == old_value:
            page.save()
            self._remove_project_page(save=False)
        try:
            project_rename.rename(self.recordings_folder, [v.meeting for v in self._kinds], old_value, new_value)
        except project_rename.NotRenamed as error:
            heading = tr("Couldn't rename the project") if new_value else tr("Couldn't delete the project")
            self.notice.emit(heading, error.text())
            if self.project == old_value:
                self._show_project_page(old_value)
            self.changed.emit()  # files that were not put back show in the list
            return False
        if new_value and self.project in (old_value, new_value):
            self._show_project_page(new_value)
        return True

    def _delete_project(self, project: str) -> None:
        answer = QMessageBox.question(
            self,
            tr("Delete project"),
            tr('Delete the project "{project}"? Its meetings stay, just without a project.', project=project),
        )
        if answer != QMessageBox.StandardButton.Yes or not self._move_project(project, ""):
            return
        self._save_projects([p for p in self._projects() if p != project])
        if self.project == project:
            self.project = None
        if project in self._project_looks:
            self._save_kind({p: v for p, v in self._project_looks.items() if p != project})
        self.changed.emit()

    def _matches(self, meetings: list[catalog.Meeting], query: str) -> dict[pathlib.Path, int]:
        """Search matches, looked for again only when the query or the meetings changed."""
        if self._found[:2] != (query, meetings):
            self._found = (query, meetings, meeting_list.find(meetings, query))
        return self._found[2]

    def _show_items(self, always: bool = False) -> None:
        query = (self._meetings_query if hasattr(self.page, "search_hint") else self.search.text()).strip()
        in_project = meeting_rows.of_project(self._kinds, self.project)
        kinds = (
            meeting_rows.found_only(in_project, self._matches([v.meeting for v in in_project], query))
            if query
            else in_project
        )
        snapshot = (query, self.project, tuple((v, v.meeting.folder.exists()) for v in kinds))
        if snapshot == self._snapshot and not always:
            return
        self._snapshot = ()  # remembered once the list is built: a rebuild cut off by an error is done again
        selected_set = {v.meeting.folder for v in self._selected_items()}
        scroll_area = self.list_.verticalScrollBar().value()
        self._rebuild_items(kinds, selected_set, scroll_area)
        if self.recordings_folder is not None and not self.recordings_folder.exists():
            self.empty.setText(
                tr(
                    "Recordings folder is unavailable\n\n{folder}\n\nIf it is on a removable or network drive, connect "
                    "it, or choose another folder in Settings",
                    folder=self.recordings_folder,
                )
            )
        elif query:
            self.empty.setText(tr("Nothing found"))
        elif self.project is not None and self._kinds:
            self.empty.setText(
                tr("No meetings in this project yet\n\nDrag a meeting here, onto the project on the left")
            )
        else:
            self.empty.setText(
                tr('Your meetings will appear here\n\nClick "Start recording" or drop a meeting video here')
            )
        self.list_.setVisible(bool(kinds))
        self.empty.setVisible(not kinds)
        self.list_.refresh_widgets()
        self._snapshot = snapshot

    def _rebuild_items(self, kinds: list[MeetingItem], selected: set[pathlib.Path], scroll: int) -> None:
        self.list_.setUpdatesEnabled(False)
        try:
            self.list_.clear()
            group = None
            for kind in kinds:
                day = tr(catalog.day(kind.meeting.date.date()))
                if day != group:
                    group = day
                    self._add_group(day)
                self._add(kind, kind.meeting.folder in selected)
            self.list_.doItemsLayout()
            self.list_.verticalScrollBar().setValue(scroll)
        finally:
            self.list_.setUpdatesEnabled(True)

    def _add_group(self, day: str) -> None:
        element = QListWidgetItem(self.list_)
        element.setFlags(Qt.ItemFlag.NoItemFlags)
        element.setData(DAY_ROLE, day)
        element.setSizeHint(QSize(0, 34))

    def _add(self, kind: MeetingItem, selected: bool) -> None:
        element = QListWidgetItem(self.list_)
        self._row(element, kind)
        element.setSelected(selected)

    def _row(self, element: QListWidgetItem, kind: MeetingItem) -> None:
        self.list_.forget_widget(element)
        element.setData(Qt.ItemDataRole.UserRole, kind)
        element.setSizeHint(QSize(0, 76 if kind.result or kind.progress else 58))

    def _row_widget(self, element: QListWidgetItem) -> MeetingRow:
        kind = element.data(Qt.ItemDataRole.UserRole)
        line = MeetingRow(kind, self._project_looks.get(kind.meeting.project, {}).get("цвет"))
        line.action.connect(lambda: self._main_point(kind))
        line.menu.connect(lambda point: self._row_menu(element, point))
        return line

    def _selected_items(self) -> list[MeetingItem]:
        return [
            e_.data(Qt.ItemDataRole.UserRole) for e_ in self.list_.selectedItems() if e_.data(Qt.ItemDataRole.UserRole)
        ]

    def _main_point(self, kind: MeetingItem | None) -> None:
        """The row button and Enter: open a ready one, transcribe the others."""
        if kind is None or kind.occupied:
            return
        if kind.meeting.status == catalog.READY:
            self.open_meeting.emit(kind.meeting.folder)
        elif kind.recognizable:
            self.recognize.emit([kind.meeting.folder])

    def _clicked_item(self, element: QListWidgetItem) -> None:
        """A click opens the meeting to the right of the list, including one not sent yet, to look at it: sending
        costs money, so it is by the button only. With Ctrl or Shift it only selects, as in Explorer."""
        if QGuiApplication.keyboardModifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        ):
            return
        kind = element.data(Qt.ItemDataRole.UserRole)
        if kind and kind.meeting.folder.is_dir():
            self.open_meeting.emit(kind.meeting.folder)

    def _row_menu(self, element: QListWidgetItem, point: QPoint) -> None:
        if not element.isSelected():
            self.list_.clearSelection()
            element.setSelected(True)
        self._menu(point)

    def _menu(self, point: QPoint) -> None:
        selected_items = self._selected_items()
        if not selected_items:
            return
        one = selected_items[0] if len(selected_items) == 1 else None
        menu = QMenu(self)
        if one and one.meeting.status == catalog.READY:
            menu.addAction(QAction(tr("Открыть расшифровку"), menu, shortcut="Enter", triggered=self._open_item))
        allowed = [v for v in selected_items if v.recognizable]
        if allowed:
            again = all(v.meeting.status == catalog.READY for v in allowed)
            row_label = (tr("Transcribe again") if again else tr("Transcribe")) + (
                f" ({len(allowed)})" if len(allowed) > 1 else ""
            )
            menu.addAction(row_label, lambda: self.recognize.emit([v.meeting.folder for v in allowed]))
        folders = [v for v in selected_items if v.meeting.folder.is_dir()]
        if one and folders:
            menu.addAction(QAction(tr("Rename"), menu, shortcut="F2", triggered=self._rename))
        if folders:
            menu.addAction(tr("Project…"), self._set_project)
        if one:
            menu.addAction(
                tr("Open folder"),
                lambda: system.open_view(
                    one.meeting.folder if one.meeting.folder.is_dir() else one.meeting.folder.parent
                ),
            )
        menu.addSeparator()
        menu.addAction(QAction(tr("Delete"), menu, shortcut="Del", triggered=self._delete))
        menu.exec(point)

    def _open_item(self) -> None:
        selected_items = self._selected_items()
        if len(selected_items) == 1:
            self._main_point(selected_items[0])

    def _rename(self) -> None:
        selected_items = [v for v in self._selected_items() if v.meeting.folder.is_dir()]
        if len(selected_items) != 1:
            return
        v = selected_items[0].meeting
        name, yes = QInputDialog.getText(self, tr("Meeting title"), tr("Title:"), text=v.display_title)
        if yes:
            catalog.set_(v.folder, title=catalog.for_storage(v.folder, name))
            self.changed.emit()

    def _set_project(self) -> None:
        selected_items = [v for v in self._selected_items() if v.meeting.folder.is_dir()]
        if not selected_items:
            return
        projects = self._projects()
        current = selected_items[0].meeting.project
        project, yes = QInputDialog.getItem(
            self,
            tr("Meeting project"),
            tr("Project (empty - no project):"),
            [current, *[p for p in projects if p != current]],
            0,
            True,
        )
        if yes:
            for v in selected_items:
                catalog.set_(v.meeting.folder, project=project.strip())
            self.changed.emit()

    def _delete(self) -> None:
        selected_items = [v for v in self._selected_items() if not v.occupied]
        if not selected_items:
            return
        question = (
            tr(
                'Move the meeting "{title}" to the Recycle Bin? The recording and transcript are deleted together, '
                "you can restore them from the Windows Recycle Bin.",
                title=selected_items[0].meeting.display_title,
            )
            if len(selected_items) == 1
            else tr(
                "Move {n} meetings to the Recycle Bin? Recordings and transcripts are deleted together, you can "
                "restore them from the Windows Recycle Bin.",
                n=len(selected_items),
            )
        )
        if QMessageBox.question(self, tr("Delete"), question) != QMessageBox.StandardButton.Yes:
            return
        if open_one := self._open_among(selected_items):
            if not self.close_meeting():  # it keeps an edit that could not be saved: it stays, the others go
                selected_items = [v for v in selected_items if v is not open_one]
            else:
                QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)  # the card lets go of its files
                self.notice.emit(
                    tr("Meeting closed"),
                    tr(
                        '"{title}" goes to the Recycle Bin, so its card is closed.',
                        title=open_one.meeting.display_title,
                    ),
                )
        if selected_items:
            failed = [v.meeting.display_title for v in selected_items if not system.to_recycle_bin(v.meeting.folder)]
            if failed:
                QMessageBox.warning(
                    self,
                    tr("Delete"),
                    tr(
                        "Could not move to the Recycle Bin: {meetings}. The files may be open in another program.",
                        meetings=", ".join(failed),
                    ),
                )
            self.changed.emit()

    def _open_among(self, kinds: list[MeetingItem]) -> MeetingItem | None:
        folder = getattr(self.meeting, "folder", None)
        return next((v for v in kinds if v.meeting.folder == folder), None)

    def pick_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, tr("Transcribe a meeting recording"), "", tr(RECORDING_FILES))
        if files:
            self.transcribe.emit([pathlib.Path(f) for f in files])

    def tips(self, key_seq: str, recognition_ready: bool) -> list[tour.Step]:
        """First-run tips: the record button, the icon by the clock and, while nothing can recognize speech yet, the
        settings."""
        how = (
            tr("Click when the meeting starts, or press {key_seq}. You and the others are recorded.", key_seq=key_seq)
            if key_seq
            else tr("Click when the meeting starts. You and the others are recorded.")
        )
        steps = [
            tour.Step(tr("Record a meeting"), how, lambda: tour.area_of(self.recording, self)),
            tour.Step(
                tr("Runs in the background"),
                tr("Close the window and the icon stays at the bottom right, by the clock."),
                icon=style.tray_icon(False).pixmap(32, 32),
            ),
        ]
        if not recognition_ready:
            steps.append(
                tour.Step(
                    tr("Speech recognition"),
                    tr("Without this setting there will be no meeting text."),
                    lambda: self._section_place("настройки"),
                    action=tr("Open settings"),
                )
            )
        return steps

    def _section_place(self, code: str) -> QRect | None:
        kind, item = self.projects_column.place(code)
        return tour.area_of(kind, self, item)

    def show_update(self, version: str) -> None:
        self.update_button.setToolTip(tr("Version {version} is out", version=version))
        self.update_button.setVisible(bool(version))

    def set_recording(self, running: bool, seconds: float = 0) -> None:
        """Record button: blue "Start recording" or red "Stop 12:34"."""
        self.recording.setText(
            "  " + (tr("Stop {time}", time=transcript.time(seconds)) if running else tr("Start recording"))
        )
        kind = "стоп" if running else "главная"
        if self.recording.objectName() != kind:
            self.recording.setObjectName(kind)
            self.recording.setIcon(style.icon(style.STOP if running else style.MICROPHONE, "#FFFFFF"))
            self.recording.setMinimumWidth(170)
            self.recording.style().unpolish(self.recording)
            self.recording.style().polish(self.recording)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        files = [pathlib.Path(address.toLocalFile()) for address in event.mimeData().urls() if address.isLocalFile()]
        files = [f for f in files if f.is_file()]
        if files:
            self.transcribe.emit(files)
