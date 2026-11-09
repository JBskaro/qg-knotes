# SPDX-License-Identifier: GPL-3.0-only
"""The card panel to the right of the task board: everything the card has is edited there and saved by itself; the
checklist of the card."""

import dataclasses
import datetime
import logging
import pathlib
from collections.abc import Callable, Sequence

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from knotes import language, projects, tasks
from knotes.ui import style
from knotes.ui.board_card import source_link
from knotes.ui.board_chips import ChipRow, chip
from knotes.ui.board_fields import DueField, TextField
from knotes.ui.board_icons import column_options, priority_options, state_options
from knotes.ui.board_labels import LabelActions, ask_label, labels_menu, toggled_labels
from knotes.ui.board_menus import cleaned, due_menu
from knotes.ui.board_writes import delete_task, failure_text, safely

log = logging.getLogger(__name__)
PANEL_WIDTH = 380
KINDS = {
    tasks.TASK: language.tr_key("Задача из протокола"),
    tasks.QUESTION: language.tr_key("Открытый вопрос"),
    tasks.DECISION: language.tr_key("Решение на подтверждении"),
}
WHO = {
    tasks.TASK: language.tr_key("Исполнитель"),
    tasks.QUESTION: language.tr_key("Кто ответит"),
    tasks.DECISION: language.tr_key("Кто подтвердит"),
}


class Checklist(QWidget):
    """Items: check box, text, cross; at the bottom a field for a new item (Enter adds it). After any edit changed()
    saves the card: a new item leaves the field only once it is saved, so a second Enter after a failure does not
    add it twice."""

    def __init__(self, changed: Callable[[], bool] = lambda: True):
        super().__init__()
        self._changed = changed
        self._items: list[tasks.ChecklistItem] = []
        self._lines = QVBoxLayout()
        self._lines.setContentsMargins(0, 0, 0, 0)
        self._lines.setSpacing(0)
        self.new = style.ClearableLine()
        self.new.setObjectName("быстрая")
        self.new.setPlaceholderText(language.tr("+  Add item"))
        self.new.returnPressed.connect(self._add_new)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        column.addLayout(self._lines)
        column.addWidget(self.new)

    def set_(self, items: tuple[tasks.ChecklistItem, ...]) -> None:
        self._items = list(items)
        self._relayout()

    def items(self) -> tuple[tasks.ChecklistItem, ...]:
        return tuple(self._items)

    def add(self, text: str) -> bool:
        """True if the item was added and saved; one that was not saved is taken back."""
        if not text.strip():
            return False
        self._items.append(tasks.ChecklistItem(text.strip()))
        if self._change():
            return True
        self._items.pop()
        self._relayout()
        return False

    def _add_new(self) -> None:
        if self.add(self.new.text()):
            self.new.clear()

    def mark(self, number: int, is_done: bool) -> None:
        self._items[number] = dataclasses.replace(self._items[number], is_done=is_done)
        self._change()

    def remove_item(self, number: int) -> None:
        self._items.pop(number)
        self._change()

    def _change(self) -> bool:
        self._relayout()
        return self._changed()

    def _relayout(self) -> None:
        style.empty(self._lines)
        for number, item in enumerate(self._items):
            self._lines.addWidget(self._line(number, item))

    def _line(self, number: int, item: tasks.ChecklistItem) -> QWidget:
        line = QWidget()
        row = QHBoxLayout(line)
        row.setContentsMargins(2, 1, 0, 1)
        checkmark = QCheckBox()
        checkmark.setChecked(item.is_done)
        checkmark.toggled.connect(lambda on, n=number: self.mark(n, on))
        text = QLabel(item.text)
        text.setTextFormat(Qt.TextFormat.PlainText)
        text.setWordWrap(True)
        if item.is_done:
            text.setObjectName("сделано")
            font = text.font()
            font.setStrikeOut(True)
            text.setFont(font)
        close_button = style.icon_button(
            style.CLOSE, language.tr("Remove item"), lambda _=False, n=number: self.remove_item(n)
        )
        close_button.setObjectName("крестик")
        row.addWidget(checkmark, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(text, 1)
        row.addWidget(close_button, 0, Qt.AlignmentFlag.AlignTop)
        return line


class TaskPanel(QFrame):
    """The whole card to the right of the board. Edits save themselves: text after half a second, choices at once,
    and everything on close. A new task appears on the board once it has a text. A failed save is shown once and the
    edits stay in the panel; a card gone from the board (its minutes were written again without it) closes the
    panel with gone(task)."""

    saved = Signal(object)  # the card as written
    removed = Signal()  # the own task was deleted
    gone = Signal(object)  # the card that is no longer on the board
    closed = Signal()
    open_place = Signal(pathlib.Path, float)
    configure_labels = Signal()

    def __init__(self, recordings: pathlib.Path):
        super().__init__()
        self.setObjectName("панель")
        self.setFixedWidth(PANEL_WIDTH)
        self.recordings = recordings
        self.task: tasks.Task | None = None  # None for a new task until it has a text
        self._project, self._saved = "", {}
        self._due: datetime.date | None = None
        self._labels: tuple[str, ...] = ()
        self._all_labels: list[tasks.Label] = []
        self._columns: dict[str, projects.Column] = {}  # the status list holds project columns; empty: bases
        self._error, self._warned, self._closing = "", False, False  # the last save failure and what it showed
        self._later = QTimer(self, singleShot=True, interval=500)
        self._later.timeout.connect(self.save)
        self._fields()
        self._build()
        self.repaint_theme()
        self.hide()

    def _fields(self) -> None:
        self.kind = QLabel()
        self.kind.setObjectName("второй_текст")
        self.heading = TextField("заголовок_задачи", language.tr("What needs to be done"), (30, 400), line=True)
        self.heading.textChanged.connect(self._later.start)
        self.status, self.priority, self.assignee = QComboBox(), QComboBox(), QComboBox()
        for field, variants in ((self.status, state_options()), (self.priority, priority_options())):
            self._fill(field, variants)
            field.currentIndexChanged.connect(lambda _number: self.save())
        self.assignee.setEditable(True)
        self.assignee.lineEdit().setPlaceholderText(language.tr("Unassigned"))
        self.assignee.editTextChanged.connect(lambda _text: self._later.start())
        self.due = DueField(lambda menu: due_menu(menu, self._due, self.set_due))
        self._labels_field()
        self._description_field()

    def _labels_field(self) -> None:
        """Labels as chips and a "+" with a menu: add or remove a label, new label, manage labels."""
        self._labels_row = ChipRow()
        self._labels_button = style.icon_button(style.PLUS, language.tr("Labels"))
        self._labels_button.setMenu(QMenu(self._labels_button))
        self._labels_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._labels_button.menu().aboutToShow.connect(
            lambda: labels_menu(
                cleaned(self._labels_button.menu()),
                self._all_labels,
                self._labels,
                LabelActions(self.toggle_label, self._new_label, self.configure_labels.emit),
            )
        )

    def _description_field(self) -> None:
        """Description, checklist, source and the archive and delete buttons."""
        self.description = TextField("описание", language.tr("Add a description"), (90, 320), line=False)
        self.description.textChanged.connect(self._later.start)
        self.heading.further.connect(self.description.setFocus)
        self.checklist = Checklist(self._checklist_changed)
        self._checklist_count = QLabel()
        self._checklist_count.setObjectName("второй_текст")
        self.origin = QLabel()
        self.origin.setWordWrap(True)
        self.origin.linkActivated.connect(lambda _link: self._to_meeting())
        self._note_mark = QLabel(language.tr("Edits show on the board only: the minutes stay as they are"))
        self._note_mark.setObjectName("тихий_текст")
        self._note_mark.setWordWrap(True)
        self.archive = QPushButton()
        self.archive.clicked.connect(self._to_archive)
        self.delete = QPushButton(language.tr("Delete"))
        self.delete.setObjectName("опасная")
        self.delete.clicked.connect(self._delete)
        self._who_caption = QLabel()

    def _to_meeting(self) -> None:
        if self.task is not None and self.task.origin.folder is not None:
            self.open_place.emit(self.task.origin.folder, float(self.task.origin.second or 0))

    @staticmethod
    def _section(text: str) -> QLabel:
        caption = QLabel(text)
        caption.setStyleSheet("font-weight: 600;")
        return caption

    def _properties(self) -> QGridLayout:
        labels = QWidget()
        labels.setLayout(self._labels_row)
        grid_layout = QGridLayout()
        grid_layout.setHorizontalSpacing(12)
        grid_layout.setVerticalSpacing(6)
        for line, (row_label, widget) in enumerate(
            (
                (QLabel(language.tr("Status")), self.status),
                (QLabel(language.tr("Priority")), self.priority),
                (self._who_caption, self.assignee),
                (QLabel(language.tr("Due date")), self.due),
                (QLabel(language.tr("Labels")), labels),
            )
        ):
            row_label.setObjectName("второй_текст")
            row_label.setMinimumWidth(96)
            grid_layout.addWidget(row_label, line, 0, Qt.AlignmentFlag.AlignVCenter)
            grid_layout.addWidget(widget, line, 1)
        grid_layout.setColumnStretch(1, 1)
        return grid_layout

    def _build(self) -> None:
        header = QHBoxLayout()
        header.addWidget(self.kind, 1)
        header.addWidget(style.icon_button(style.CLOSE, language.tr("Close  Esc"), self.close_view))
        contents_ = QWidget()
        contents_.setObjectName("содержимое_панели")
        column = QVBoxLayout(contents_)
        column.setContentsMargins(0, 0, 6, 0)
        column.setSpacing(8)
        column.addWidget(self.heading)
        column.addLayout(self._properties())
        column.addSpacing(6)
        column.addWidget(self._section(language.tr("Description")))
        column.addWidget(self.description)
        column.addSpacing(6)
        list_header = QHBoxLayout()
        list_header.addWidget(self._section(language.tr("Checklist")))
        list_header.addWidget(self._checklist_count)
        list_header.addStretch()
        column.addLayout(list_header)
        column.addWidget(self.checklist)
        self._source_block = QWidget()
        origin = QVBoxLayout(self._source_block)
        origin.setContentsMargins(0, 6, 0, 0)
        for widget in (self._section(language.tr("Source")), self.origin, self._note_mark):
            origin.addWidget(widget)
        column.addWidget(self._source_block)
        column.addStretch()
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setWidget(contents_)
        bottom = QHBoxLayout()
        bottom.addWidget(self.archive)
        bottom.addStretch()
        bottom.addWidget(self.delete)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 8, 8, 12)
        outer.setSpacing(6)
        outer.addLayout(header)
        outer.addWidget(scroll_area, 1)
        outer.addLayout(bottom)

    @staticmethod
    def _fill(field: QComboBox, variants: Sequence[tuple[object, str, QIcon]]) -> None:
        field.blockSignals(True)
        field.clear()
        for value, row_label, icon in variants:
            field.addItem(icon, row_label, value)
        field.blockSignals(False)

    def _status_options(self) -> list[tuple[str, str, QIcon]]:
        return column_options(list(self._columns.values())) if self._columns else state_options()

    def set_columns(self, columns: Sequence[projects.Column] | None) -> None:
        """The columns of the project board for the status list; None: the board of all projects, the three bases.
        The open card keeps its column if it is still there; the list changing is not an edit of the card."""
        current = self.status.currentData()
        self._columns = {c.key: c for c in columns or ()}
        self._fill(self.status, self._status_options())
        self.status.blockSignals(True)
        self.status.setCurrentIndex(max(0, self.status.findData(current)))
        self.status.blockSignals(False)
        if self._saved:
            self._saved["status"] = self.status.currentData()

    def repaint_theme(self) -> None:
        """Status and priority signs and label colors follow the theme."""
        for field, variants in ((self.status, self._status_options()), (self.priority, priority_options())):
            for number, (_val, _row_label, icon) in enumerate(variants):
                field.setItemIcon(number, icon)
        self._show_labels()
        if self.task is not None:
            self.origin.setText(source_link(self.task))

    # --- display -------------------------------------------------------------------------------------------

    def key(self) -> str:
        """Key of the open card; empty if the panel is closed or the task is new."""
        return self.task.key if self.task is not None and not self.isHidden() else ""

    def show_card(
        self, task: tasks.Task | None, assignees: list[str], labels: list[tasks.Label], project: str = ""
    ) -> bool:
        """Opens the card in the panel, saving the previous one; None starts a new task in the project. False if the
        previous card was not saved: it stays in the panel."""
        if not self._release():
            return False
        self.task, self._project, self._all_labels = task, project, labels
        template = task or tasks.Task(
            "", "", "", None, tasks.TASK, project, tasks.TODO, tasks.Source(None, tasks.MANUAL, datetime.date.today())
        )
        fields = (self.heading, self.status, self.priority, self.assignee, self.description)
        for field in fields:
            field.blockSignals(True)
        self.heading.setPlainText(template.text)
        place = template.column if self._columns and template.column in self._columns else template.status
        if task is None and self._columns:  # a new task goes to the first column
            place = next(iter(self._columns))
        self.status.setCurrentIndex(max(0, self.status.findData(place)))
        self.priority.setCurrentIndex(max(0, self.priority.findData(template.priority)))
        self.assignee.clear()
        self.assignee.addItems(["", *assignees])
        self.assignee.setCurrentText(template.who)
        self.description.setPlainText(template.description)
        for field in fields:
            field.blockSignals(False)
        self._due, self._labels = template.due, template.labels
        self.checklist.set_(template.checklist)
        self._show_due()
        self._show_labels()
        self._show_checklist_count()
        self._show_kind(template)
        self._saved = self._values()
        self._error, self._warned, self._closing = "", False, False
        self.show()
        if task is None:
            self.heading.setFocus()
        return True

    def set_labels(self, labels: list[tasks.Label], at_card: tuple[str, ...] | None = None) -> None:
        """The recordings folder labels changed; at_card also gives the open card's labels (renamed or deleted)."""
        self._all_labels = labels
        if at_card is not None and self._saved:
            self._labels = self._saved["labels"] = at_card
        self._show_labels()

    def _show_kind(self, task: tasks.Task) -> None:
        if self.task is None:
            kind = language.tr("New task")
        else:
            kind = language.tr("Own task") if task.custom else language.tr(KINDS.get(task.kind, KINDS[tasks.TASK]))
        self.kind.setText(kind + (" · " + language.tr("archived") if task.archive else ""))
        self._who_caption.setText(language.tr(WHO.get(task.kind, WHO[tasks.TASK])))
        exists = self.task is not None
        self._source_block.setVisible(exists)
        self.origin.setText(source_link(task))
        self._note_mark.setVisible(exists and not task.custom)
        self.archive.setVisible(exists)
        self.archive.setText(language.tr("Restore from archive") if task.archive else language.tr("В архив"))
        self.delete.setVisible(exists and task.custom)

    def _show_due(self) -> None:
        self.due.display(f"{self._due:%d.%m.%Y}" if self._due else language.tr("No due date"))

    def _show_labels(self) -> None:
        style.empty(self._labels_row, keep=(self._labels_button,))
        colors = {m.name: m.color for m in self._all_labels}
        for name in self._labels:
            self._labels_row.addWidget(chip(name, colors.get(name, 0)))
        self._labels_row.addWidget(self._labels_button)

    def _show_checklist_count(self) -> None:
        items = self.checklist.items()
        self._checklist_count.setText(f"{sum(p.is_done for p in items)}/{len(items)}" if items else "")

    # --- editing -------------------------------------------------------------------------------------------

    def _values(self) -> dict:
        """Panel fields as edit fields."""
        return {
            "text": self.heading.toPlainText().strip(),
            "description": self.description.toPlainText().rstrip(),
            "who": self.assignee.currentText().strip(),
            "due": self._due or tasks.NO_DUE,
            "labels": self._labels,
            "priority": self.priority.currentData(),
            "status": self.status.currentData(),
            "checklist": self.checklist.items(),
        }

    def _edit(self, values: dict) -> tasks.Edit:
        """Panel values as a card edit: the status list holds a column of the project board or a base."""
        fields = dict(values)
        if (place := fields.pop("status", None)) is not None:
            column = self._columns.get(place)
            fields |= {"status": column.base, "column": column.key} if column else {"status": place}
        return tasks.Edit(**fields)

    def _write(self, values: dict, edit_item: dict) -> tasks.Task:
        if self.task is None:
            task = tasks.add(
                self.recordings,
                values["text"],
                self._project,
                self._edit({p: z for p, z in values.items() if p != "text"}),
            )
            self._show_kind(task)
            return task
        tasks.edit(self.recordings, self.task.key, self._edit(edit_item))
        return tasks.refreshed(self.recordings, self.task) or self.task

    def save(self) -> bool:
        """Saves what changed since last time to the board; a task without a text is not saved. True if saved."""
        self._later.stop()
        if not self._saved:
            return False
        values = self._values()
        edit_item = {p: z for p, z in values.items() if z != self._saved[p] and (p != "text" or z)}
        if not edit_item or (self.task is None and not values["text"]):
            return False
        try:
            self.task = self._write(values, edit_item)
        except tasks.GoneError:
            self._gone()
            return False
        except (OSError, ValueError) as error:
            self._failed(error)
            return False
        self._saved, self._error, self._warned = values, "", False
        self.saved.emit(self.task)
        return True

    def _failed(self, error: Exception) -> None:
        """Logged every time, shown once until a save succeeds: autosave tries again after each pause in typing."""
        log.warning("the card was not saved: %s", error)
        self._error = failure_text(error)
        if not self._warned and not self._closing:
            self._warned = True
            style.ask(
                self, language.tr("Tasks"), language.tr("The card was not saved: {reason}", reason=self._error), True
            )

    def _gone(self) -> None:
        task = self.task
        self.discard()
        self.gone.emit(task)

    def set_due(self, due: datetime.date | None) -> None:
        self._due = due
        self._show_due()
        self.save()

    def toggle_label(self, name: str) -> None:
        self._labels = toggled_labels(self._labels, name)
        self._show_labels()
        self.save()

    def _new_label(self) -> None:
        if (name := ask_label(self)) and name not in self._labels:
            self.toggle_label(name)

    def _checklist_changed(self) -> bool:
        """True unless the save failed: then the checklist takes back a new item."""
        self._show_checklist_count()
        self.save()
        return not self._error

    def _to_archive(self) -> None:
        if self.task is None:
            return
        self.save()
        task, archive = self.task, not self.task.archive
        if safely(self, lambda: tasks.edit(self.recordings, task.key, tasks.Edit(archive=archive))):
            self.task = dataclasses.replace(task, archive=archive)
            self._show_kind(self.task)
            self.saved.emit(self.task)

    def _delete(self) -> None:
        if self.task is None or not delete_task(self, self.recordings, self.task):
            return
        self._later.stop()
        self.task, self._saved = None, {}
        self.hide()
        self.removed.emit()
        self.closed.emit()

    def _release(self) -> bool:
        """Saves the card before the panel lets it go. If the save failed, the card stays once more so the text can
        be copied, and letting it go again discards the edits. True if the panel may take another card or close."""
        self.save()
        if not self._error or self._closing:
            return True
        self._closing = True
        style.ask(
            self,
            language.tr("Tasks"),
            language.tr(
                "The card was not saved: {reason} The panel keeps it so you can copy the text; close it again to "
                "discard the changes.",
                reason=self._error,
            ),
            warning=True,
        )
        return False

    def close_view(self) -> None:
        if self._release():
            self.discard()

    def discard(self) -> None:
        """Closes without saving: the card is gone or its edits were given up."""
        self._later.stop()
        self.task, self._saved, self._error = None, {}, ""
        self.hide()
        self.closed.emit()
