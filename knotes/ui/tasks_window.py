# SPDX-License-Identifier: GPL-3.0-only
"""The "Tasks" page: a board of tasks, remarks, questions and tentative decisions from the minutes plus own tasks,
in columns. A project board has the project's columns ("To do", "In progress", "Done" and the ones added, each
counting as one of these three); the board of all projects has the three, a card of a project column carries its
name.

A card is dragged with the mouse to another column or another place in the column; the right button opens a menu:
column, priority, labels, due date, assignee, archive. "Set up the board", on any board, opens the board settings:
the columns of a project and the labels; a double click on a column name of a project board edits it in place. The
search is the top bar of the meetings window, lent to the board. A click on a card opens a panel on the right:
everything the card has is edited there and saved by itself. At the bottom of a column is a quick add line. The
source is the meeting, date and minute the task came from: a click opens the meeting at that place.

The page puts together the parts in modules of their own: the card (board_card), the column and its header
(board_columns), the board settings (board_settings with board_columns_page and the labels tab in board_labels), the
card panel (board_panel), the filters (board_filters), the labels (board_labels), the menus (board_menus), the edit
fields (board_fields), the signs (board_icons), the chips (board_chips), the style sheet (board_style) and the writes
(board_writes).
"""

import datetime
import logging
import pathlib
from collections.abc import Callable, Sequence

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QCursor, QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from knotes import catalog, failures, meeting_list, projects, tasks, language
from knotes.ui import board_filters, board_style, style
from knotes.ui.board_card import CardLook
from knotes.ui.board_columns import COLUMN_WIDTH, ColumnTitle, TaskColumn, column_header
from knotes.ui.board_fields import AddField
from knotes.ui.board_icons import column_options, priority_options, state_options, state_sign, tone
from knotes.ui.board_labels import LabelActions, ask_label, labels_menu, toggled_labels
from knotes.ui.board_menus import choice_menu, due_calendar, due_menu
from knotes.ui.board_panel import TaskPanel
from knotes.ui.board_settings import COLUMNS, LABELS, BoardSettings
from knotes.ui.board_writes import delete_task, safely

log = logging.getLogger(__name__)
SHOWN = 50  # cards a "done" column shows at first and adds with each "Show more"


class TasksWindow(QWidget):
    """The board for the chosen project (None for all, "" for no project). The open_place(folder, second) signal
    comes from a click on a card source.

    The columns show the cards in memory (_lay_out); filters, search, archive, "Show more" and the theme only lay
    them out again. The cards come into memory in these ways:
    - display(): reads them from the folder; on choosing a project, after a quick add and after the labels dialog;
    - _refresh(): display() without the meeting list after an edit from the board, once the click or menu has
      finished (_refresh_later); the card open in the panel is taken fresh;
    - _show_saved(): the cards the panel saved replace theirs without reading the folder (_show_saved_later);
    - _board_changed(): the columns or labels were changed: the columns are built again, then _refresh()
      (_board_changed_later)."""

    open_place = Signal(pathlib.Path, float)
    search_hint_changed = Signal(str)  # the board's own project button changed what it searches

    def __init__(
        self, recordings: pathlib.Path, projects_: tuple[Sequence[str], dict[str, dict]], project: str | None = None
    ):
        super().__init__()
        self.recordings, self.project = recordings, project
        self._projects_list, self._project_looks = list(projects_[0]), projects_[1]
        self._cards: list[tasks.Task] = []
        self._meetings: list[catalog.Meeting] | None = None  # an edit on the board does not change the meetings
        self._labels: list[tasks.Label] = []
        self._marks: dict[str, str] = {}  # board of all projects: card key -> its project column
        self._column_list: list[projects.Column] = list(projects.BASE_COLUMNS)
        self._shown: dict[str, int] = {}  # how many cards a "done" column shows, by column key
        self._project_button = QPushButton()
        self._project_button.setObjectName("отбор")
        self.filters = board_filters.filter_buttons()
        for button in self.filters.values():
            button.selected.connect(lambda _val: self._lay_out())
        self._query = ""  # typed in the top bar of the meetings window
        self._search_delay = QTimer(self, singleShot=True, interval=250)
        self._search_delay.timeout.connect(self._lay_out)
        self.settings_button = QPushButton(language.tr("Set up the board"))
        self.settings_button.clicked.connect(lambda: self.open_settings(COLUMNS))
        self.archive = QPushButton(language.tr("Архив"))
        self.archive.setObjectName("отбор")
        self.archive.setCheckable(True)
        self.archive.setToolTip(language.tr("Show archived cards"))
        self.archive.toggled.connect(self._show_archive)
        self.columns: dict[str, TaskColumn] = {}
        self._counters: dict[str, QLabel] = {}
        self._signs: dict[str, QLabel] = {}
        self._titles: dict[str, ColumnTitle] = {}
        self.adding: dict[str, AddField] = {}
        self._more: dict[str, QPushButton] = {}
        self._row = QHBoxLayout()
        self.panel = TaskPanel(recordings)
        self._edited = ""  # the card edited from the board: the panel takes it fresh after the redraw
        self._refresh_later = QTimer(self, singleShot=True, interval=0)  # after a click or menu, not during it
        self._refresh_later.timeout.connect(self._refresh)
        self._saved_cards: dict[str, tasks.Task] = {}
        self._show_saved_later = QTimer(self, singleShot=True, interval=0)
        self._show_saved_later.timeout.connect(self._show_saved)
        self._board_changed_later = QTimer(self, singleShot=True, interval=0)  # not while a header field is finishing
        self._board_changed_later.timeout.connect(self._board_changed)
        self._connect_signals()
        self._build()
        self.repaint_theme()
        self._pick_project(project)

    def _connect_signals(self) -> None:
        self.panel.saved.connect(self._card_saved)
        self.panel.removed.connect(self._refresh_later.start)
        self.panel.gone.connect(self._card_gone)
        self.panel.closed.connect(self._mark_open)
        self.panel.open_place.connect(self.open_place)
        self.panel.configure_labels.connect(self.configure_labels)

    def _build(self) -> None:
        add = QPushButton(language.tr("+  Task"))
        add.clicked.connect(self.new_task)
        top = QHBoxLayout()
        top.setSpacing(6)
        for widget in (self._project_button, *self.filters.values(), self.archive):
            top.addWidget(widget)
        top.addStretch()
        top.addWidget(self.settings_button)
        top.addWidget(add)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(8)
        holder = QWidget()
        holder.setObjectName("доска")
        holder.setLayout(self._row)
        scroll_area = QScrollArea()  # many project columns scroll sideways, the card panel stays
        scroll_area.setObjectName("доска")
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setWidget(holder)
        board = QHBoxLayout()
        board.setSpacing(8)
        board.addWidget(scroll_area, 1)
        board.addWidget(self.panel)
        column = QVBoxLayout(self)
        column.setContentsMargins(16, 12, 16, 16)
        column.setSpacing(10)
        column.addLayout(top)
        column.addLayout(board, 1)

    # --- columns -------------------------------------------------------------------------------------------

    def _set_columns(self) -> None:
        """The columns of the chosen board: the project's own, the three bases on the board of all projects."""
        self._column_list = (
            list(projects.BASE_COLUMNS) if self.project is None else projects.columns(self.recordings, self.project)
        )
        style.empty(self._row)
        self.columns, self._counters, self._signs, self._titles, self.adding, self._more = {}, {}, {}, {}, {}, {}
        for column in self._column_list:
            self._row.addWidget(self._column_block(column), 1)
        self.panel.set_columns(None if self.project is None else self._column_list)
        self._show_archive(self.archive.isChecked())

    def _column_block(self, column: projects.Column) -> QFrame:
        key = column.key
        col = TaskColumn(column)
        col.moved_item.connect(self._move)
        col.menu.connect(lambda task, where: self._task_menu(task).exec(where))
        col.open_card.connect(self.open_task)
        col.to_archive.connect(self._to_archive)
        col.open_place.connect(self.open_place)
        field = AddField(key, language.tr("+  Add task"))
        field.add.connect(self._quick_add)
        more = QPushButton()
        more.setObjectName("еще")
        more.clicked.connect(lambda: self._show_more(key))
        more.hide()
        self.columns[key], self.adding[key], self._more[key] = col, field, more
        header = column_header(column, None if self.project is None else lambda name: self._rename_column(key, name))
        self._signs[key], self._titles[key], self._counters[key] = header.sign, header.title, header.counter
        block = style.layer(header.widget, col, more, field, padding=6)
        block.setMinimumWidth(COLUMN_WIDTH)
        return block

    def _rename_column(self, key: str, name: str) -> None:
        project = self.project or ""
        self._change_columns(lambda: projects.rename_column(self.recordings, project, key, name))

    def board_settings(self, tab: str = COLUMNS) -> BoardSettings:
        """The board settings on the project of the board, the first project on the board of all projects; every
        change they write builds the board again."""
        project = self.project if self.project is not None else next(iter(self._projects_list), "")
        dialog = BoardSettings(self, self.recordings, (self._projects_list, self._project_looks), project, tab)
        dialog.changed.connect(self._board_changed_later.start)
        return dialog

    def open_settings(self, tab: str) -> None:
        self.panel.save()
        self.board_settings(tab).exec()

    def configure_labels(self) -> None:
        self.open_settings(LABELS)

    def _change_columns(self, action: Callable[[], object]) -> None:
        self.panel.save()
        if safely(self, action):
            self._board_changed_later.start()

    def _board_changed(self) -> None:
        """The columns or labels changed: the board is built again, the card open in the panel is taken fresh (it
        may have moved with a deleted column or lost a deleted label)."""
        self._set_columns()
        self._edited = self.panel.key()
        self._refresh()

    def repaint_theme(self) -> None:
        self.setStyleSheet(board_style.sheet())
        self.settings_button.setIcon(style.icon(style.SETTINGS, tone()))
        for key, sign in self._signs.items():
            sign.setPixmap(state_sign(self.columns[key].column.base, tone()))
        self.panel.repaint_theme()
        self._refresh_filters()
        self._lay_out()

    # --- filters and display -------------------------------------------------------------------------------

    def _pick_project(self, project: str | None) -> None:
        self.panel.close_view()
        if not self.panel.isHidden():  # the card was not saved: the panel stays until closed again
            return
        self.project, self._meetings, self._shown = project, None, {}
        name = language.tr("All projects") if project is None else project or language.tr("No project")
        self._project_button.setText(style.menu_text(name))
        self._project_button.setMenu(
            style.projects_menu(self, self._projects_list, self._project_looks, project, self._pick_project)
        )
        heading = language.tr("Tasks") if project is None else language.tr("Tasks - {project}", project=name)
        self.window().setWindowTitle(f"{heading} - {style.NAME}")
        self.search_hint_changed.emit(self.search_hint)
        self._set_columns()
        self.display()

    def _load(self, reread: bool) -> None:
        """The cards and labels; a folder that cannot be read (a network drive gone) gives an empty board and says
        why."""
        try:
            if reread or self._meetings is None:
                self._meetings = catalog.list_(self.recordings)
            self._cards = tasks.tasks(self.recordings, self.project, self._meetings)
            self._labels = tasks.labels(self.recordings)
            self._marks = tasks.column_marks(self.recordings, self._cards) if self.project is None else {}
        except OSError as error:
            log.warning("the board was not read: %s", error)
            self._meetings, self._cards, self._labels, self._marks = [], [], [], {}
            style.ask(self, language.tr("Tasks"), failures.text(error), warning=True)

    def display(self, reread: bool = True) -> None:
        """Rebuilds the board from the minutes and own tasks; reread: the meeting list too, an edit on the board does
        not change it. The card open in the panel may be gone: its minutes were written again without it."""
        self._load(reread)
        opened = self.panel.task
        if opened is not None and not self.panel.isHidden() and all(z.key != opened.key for z in self._cards):
            self.panel.discard()
            self._card_gone(opened)
        self._refresh_filters()
        self.panel.set_labels(self._labels)
        self._lay_out()

    def _refresh_filters(self) -> None:
        board_filters.set_variants(self.filters, self._cards, self._labels)

    @property
    def search_hint(self) -> str:
        if self.project is None:
            return language.tr("Search all tasks  Ctrl+F")
        if not self.project:
            return language.tr("Search tasks without a project  Ctrl+F")
        return language.tr("Search tasks of project {project}  Ctrl+F", project=self.project)

    def search(self, query: str) -> None:
        """The query from the top bar; the cards are laid out again once typing pauses."""
        self._query = query
        self._search_delay.start()

    def _filter(self) -> tasks.Filter:
        return board_filters.chosen(self.filters, self._query, self.archive.isChecked())

    def _place_of(self, task: tasks.Task) -> str:
        """The column key of a card: its project column, on the board of all projects its state."""
        return task.status if self.project is None else task.column

    def _cards_of(self, key: str, list_: list[tasks.Task], today: datetime.date) -> list[tasks.Task]:
        return tasks.in_order([z for z in list_ if self._place_of(z) == key], today)

    def _limit(self, key: str) -> int | None:
        """A "done" column grows without end: it shows the first cards and more on "Show more"."""
        return self._shown.get(key, SHOWN) if self.columns[key].column.base == tasks.DONE else None

    def _card_look(self, today: datetime.date) -> CardLook:
        return CardLook(today, {m.name: m.color for m in self._labels}, self.panel.key(), self._marks)

    def _lay_out(self, only: str = "") -> None:
        """Filtered cards into the columns: those without a place set by hand first (overdue on top, then by due
        date, newer above), then the rest as they were rearranged."""
        today = datetime.date.today()
        visible = {z.key for z in tasks.pick_out(self._cards, self._filter(), today)}
        look = self._card_look(today)
        found = meeting_list.search_pattern(self._query.strip()) if self._query.strip() else None
        for key, col in self.columns.items():
            if only and key != only:
                continue
            whole = self._cards_of(key, self._cards, today)
            count = sum(z.key in visible for z in whole)
            rest = count - col.set_(whole, look, visible, self._limit(key))
            col.light_up(found)
            self._counters[key].setText(str(count))
            self._more[key].setVisible(rest > 0)
            self._more[key].setText(language.tr("Show more ({n})", n=rest))

    def _show_more(self, key: str) -> None:
        self._shown[key] = self._shown.get(key, SHOWN) + SHOWN
        self._lay_out(key)

    def _show_archive(self, on: bool) -> None:
        for field in self.adding.values():
            field.setHidden(on)
        self._lay_out()

    def _mark_open(self) -> None:
        for col in self.columns.values():
            col.mark(self.panel.key())

    # --- actions -------------------------------------------------------------------------------------------

    def _card_saved(self, task: tasks.Task) -> None:
        """The panel wrote the card: it is drawn once the click or menu has finished."""
        self._saved_cards[task.key] = task
        self._show_saved_later.start()

    def _show_saved(self) -> None:
        """The board is not read again: the saved cards replace theirs, and a column draws only the cards that
        changed, unless the order changed."""
        saved, self._saved_cards = self._saved_cards, {}
        known = {z.key for z in self._cards}
        self._cards = [saved.get(z.key, z) for z in self._cards] + [z for k, z in saved.items() if k not in known]
        labels = tasks.labels(self.recordings)
        if labels != self._labels:
            self._labels = labels
            self.panel.set_labels(labels)
        self._refresh_filters()
        self._lay_out()

    def _card_gone(self, task: tasks.Task) -> None:
        """The card open in the panel is no longer on the board: the panel is closed and its edits are not written
        to nowhere."""
        style.ask(
            self,
            language.tr("Tasks"),
            language.tr(
                "The card “{text}” is no longer on the board: its minutes were written again without it. The card "
                "panel is closed.",
                text=task.text,
            ),
            warning=True,
        )
        self._refresh_later.start()

    def _refresh(self) -> None:
        """After an edit from the board: the cards again, without the meeting list; the card open in the panel too."""
        key, self._edited = self._edited, ""
        self.display(reread=False)
        fresh = next((z for z in self._cards if z.key == key), None) if key else None
        if fresh is not None and self.panel.key() == key:
            self.panel.show_card(fresh, tasks.assignees(self._cards), self._labels)

    def open_task(self, task: tasks.Task) -> None:
        if self.panel.show_card(task, tasks.assignees(self._cards), self._labels):
            self._mark_open()

    def new_task(self) -> None:
        self.panel.show_card(None, tasks.assignees(self._cards), self._labels, self.project or "")
        self._mark_open()

    def _after_edit(self, key: str) -> None:
        """Redraws the board once the click or menu has finished; the card open in the panel too."""
        self._edited = key
        self._refresh_later.start()

    def _edit(self, task: tasks.Task, edit_item: tasks.Edit) -> None:
        self.panel.save()
        safely(self, lambda: tasks.edit(self.recordings, task.key, edit_item))
        self._after_edit(task.key)

    def _move(self, key: str, column_key: str, place: int) -> None:
        """Puts the card into the column at the place among the shown cards; the order is counted over the whole
        column, with the cards a filter or "Show more" hides, so they keep their places."""
        shown = self.columns[column_key].keys()
        if key in shown and place in (shown.index(key), shown.index(key) + 1):
            return
        before = next((k for k in shown[place:] if k != key), None)
        after = next((k for k in reversed(shown[:place]) if k != key), None)
        column = self.columns[column_key].column
        whole = [z.key for z in self._cards_of(column_key, self._cards, datetime.date.today())]
        keys = tasks.placed(whole, key, before, after)
        self.panel.save()
        safely(
            self,
            lambda: tasks.reorder(self.recordings, column.base, keys, None if self.project is None else column.key),
        )
        self._after_edit(key)

    def _quick_add(self, text: str, key: str) -> None:
        """A task from the line at the bottom of a column goes to its end; the assignee, label and priority filters
        are applied to it. The line is cleared once the task is written."""
        filter_, column = self._filter(), self.columns[key].column
        whole = [z for z in self._cards if self._place_of(z) == key]
        places = [z.order for z in whole if z.order is not None]
        edit_item = tasks.Edit(
            who=filter_.who,
            labels=(filter_.label,) if filter_.label else None,
            priority=filter_.priority,
            status=column.base,
            order=max(places, default=-1) + 1,
            column=None if self.project is None else key,
        )
        if not safely(self, lambda: tasks.add(self.recordings, text, self.project or "", edit_item)):
            return
        self.adding[key].clear()
        self._shown[key] = max(self._shown.get(key, SHOWN), len(whole) + 1)  # the new card at the end is shown
        self.display(reread=False)
        self.columns[key].scrollToBottom()

    def _delete(self, task: tasks.Task) -> None:
        if delete_task(self, self.recordings, task):
            self._after_edit(task.key)

    def _to_archive(self, task: tasks.Task) -> None:
        """Delete on a card archives it; in the archive it does nothing."""
        if not task.archive:
            self._edit(task, tasks.Edit(archive=True))

    def _status_menu(self, menu: QMenu, task: tasks.Task) -> None:
        """The columns of the project board, on the board of all projects the three states."""
        if self.project is None:
            choice_menu(menu, state_options(), task.status, lambda s: self._edit(task, tasks.Edit(status=s)))
            return
        bases = {c.key: c.base for c in self._column_list}
        choice_menu(
            menu,
            column_options(self._column_list),
            task.column,
            lambda k: self._edit(task, tasks.Edit(status=bases[k], column=k)),
        )

    def _task_menu(self, task: tasks.Task) -> QMenu:
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        def edit(**fields: object) -> None:
            self._edit(task, tasks.Edit(**fields))

        menu.addAction(language.tr("Open card"), lambda: self.open_task(task))
        menu.addSeparator()
        self._status_menu(menu.addMenu(language.tr("Status")), task)
        choice_menu(
            menu.addMenu(language.tr("Priority")), priority_options(), task.priority, lambda p: edit(priority=p)
        )
        labels_menu(
            menu.addMenu(language.tr("Labels")),
            self._labels,
            task.labels,
            LabelActions(
                lambda name: edit(labels=toggled_labels(task.labels, name)),
                lambda: self._new_label(task),
                self.configure_labels,
            ),
        )
        due_menu(
            menu.addMenu(language.tr("Due date")),
            task.due,
            lambda due: edit(due=due or tasks.NO_DUE),
            lambda: self._pick_day(task),
        )
        who = choice_menu(
            menu.addMenu(language.tr("Assignee")),
            [("", language.tr("Unassigned"), None), *((i, i, None) for i in tasks.assignees(self._cards))],
            task.who,
            lambda i: edit(who=i),
        )
        who.addSeparator()
        who.addAction(language.tr("Someone else…"), lambda: self._other_assignee(task))
        menu.addSeparator()
        if task.origin.folder is not None:
            menu.addAction(
                language.tr("Open the meeting at this point"),
                lambda: self.open_place.emit(task.origin.folder, float(task.origin.second or 0)),
            )
        menu.addAction(
            language.tr("Restore from archive") if task.archive else language.tr("В архив"),
            lambda: edit(archive=not task.archive),
        )
        if task.custom:
            menu.addAction(language.tr("Delete…"), lambda: self._delete(task))
        return menu

    def _new_label(self, task: tasks.Task) -> None:
        if (name := ask_label(self)) and name not in task.labels:
            self._edit(task, tasks.Edit(labels=(*task.labels, name)))

    def _other_assignee(self, task: tasks.Task) -> None:
        name, yes = QInputDialog.getText(self, language.tr("Assignee"), language.tr("Who does it"), text=task.who)
        if yes:
            self._edit(task, tasks.Edit(who=name))

    def _pick_day(self, task: tasks.Task) -> None:
        """A calendar popup at the pointer; the chosen day goes straight into the card."""

        def selected(day: datetime.date) -> None:
            calendar.close()
            self._edit(task, tasks.Edit(due=day))

        calendar = due_calendar(task.due, selected)
        calendar.setParent(self, Qt.WindowType.Popup)
        calendar.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        calendar.move(QCursor.pos())
        calendar.show()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Esc anywhere on the page closes the card panel."""
        if event.key() == Qt.Key.Key_Escape and not self.panel.isHidden():
            self.panel.close_view()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.panel.save()
        super().closeEvent(event)
