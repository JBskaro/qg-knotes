# SPDX-License-Identifier: GPL-3.0-only
"""The Columns tab of the board settings: the project to set up, order by dragging, the name in place, the
category, delete with a place for the cards. Every change is written at once, as the other settings."""

import collections
import pathlib
from collections.abc import Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QDropEvent, QIcon, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from knotes import language, projects, tasks
from knotes.ui import style
from knotes.ui.board_icons import state_options, state_sign, tone
from knotes.ui.board_menus import choice_menu
from knotes.ui.board_settings_page import CARDS, SettingsPage, count_label, free_name, trash_button

GRIP = ""  # icon font: "GripperBarVertical"
KEY = Qt.ItemDataRole.UserRole


class ColumnList(QListWidget):
    """The columns from left to right as rows from top to bottom. A dragged row is not moved by the list itself: the
    order goes to the data and the list is built again, as on the board."""

    moved = Signal(str, int)  # column key, its new place

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("колонки_доски")
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDropIndicatorShown(True)
        self.setSpacing(1)

    def dropEvent(self, event: QDropEvent) -> None:
        dragged = self.currentItem()
        target = self.itemAt(event.position().toPoint())
        place = self.count() if target is None else self.row(target)
        if target is not None and self.dropIndicatorPosition() == QAbstractItemView.DropIndicatorPosition.BelowItem:
            place += 1
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        if dragged is not None:
            source = self.row(dragged)
            self.moved.emit(dragged.data(KEY), place - 1 if place > source else place)


def ask_target(
    parent_: QWidget, column: projects.Column, others: list[projects.Column], cards: int
) -> projects.Column | None:
    """Where the cards of the deleted column go, by default the first other column of its category; None if
    cancelled."""
    box = QDialog(parent_)
    box.setWindowTitle(language.tr("Delete column"))
    question = QLabel(language.tr("Delete column “{name}”?", name=projects.column_name(column)))
    question.setStyleSheet("font-weight: 600;")
    where = QLabel(language.tr("It has {cards}. Move them to", cards=language.count(cards, CARDS)))
    choice = QComboBox()
    for other in others:
        choice.addItem(QIcon(state_sign(other.base, tone())), style.menu_text(projects.column_name(other)))
    choice.setCurrentIndex(next(n for n, c in enumerate(others) if c.base == column.base))
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
    buttons.addButton(language.tr("Delete"), QDialogButtonBox.ButtonRole.AcceptRole)
    buttons.accepted.connect(box.accept)
    buttons.rejected.connect(box.reject)
    layout = QVBoxLayout(box)
    for widget in (question, where, choice, buttons):
        if isinstance(widget, QLabel):
            widget.setTextFormat(Qt.TextFormat.PlainText)  # a column name is outside text
        layout.addWidget(widget)
    box.setStyleSheet(style.window_style())
    return others[choice.currentIndex()] if box.exec() == QDialog.DialogCode.Accepted else None


def project_choice(projects_: tuple[Sequence[str], dict[str, dict]], project: str) -> QComboBox:
    """The projects with their icons, then "No project" (""); project is chosen."""
    choice = QComboBox()
    color = choice.palette().color(QPalette.ColorRole.WindowText)
    names, looks = projects_
    for name in names:
        choice.addItem(style.project_icon(looks.get(name, {}), color), name, name)
    choice.addItem(style.icon(style.FOLDER, color), language.tr("No project"), "")
    if choice.findData(project) < 0:  # a project not in the list yet: shown all the same
        choice.insertItem(0, project, project)
    choice.setCurrentIndex(choice.findData(project))
    return choice


class ColumnsPage(SettingsPage):
    """The columns of the chosen project; the project can be changed right here."""

    def __init__(self, recordings: pathlib.Path, projects_: tuple[Sequence[str], dict[str, dict]], project: str):
        choice = project_choice(projects_, project)
        top = QWidget()
        row = QHBoxLayout(top)
        row.setContentsMargins(0, 0, 0, 4)
        caption = QLabel(language.tr("Project"))
        caption.setObjectName("второй_текст")
        row.addWidget(caption)
        row.addWidget(choice)
        row.addStretch()
        note = language.tr(
            "Each project has its own columns; the board of all projects shows the three base ones. The category "
            "decides what counts as done: done cards do not go into the agenda and the open tasks count. The last "
            "column of a category can't be deleted."
        )
        super().__init__(ColumnList(), language.tr("+  Add column"), note, top)
        self.recordings, self.project, self.choice = recordings, project, choice
        choice.currentIndexChanged.connect(self._pick)
        self.list_.moved.connect(self._move)
        self.list_.setMinimumSize(440, 200)
        self.refresh()

    def _pick(self) -> None:
        self.project = self.choice.currentData()
        self.refresh()

    def _columns(self) -> list[projects.Column]:
        return projects.columns(self.recordings, self.project)

    def refresh(self) -> None:
        list_ = self._columns()
        cards = collections.Counter(z.column for z in tasks.tasks(self.recordings, self.project))
        self.clear_rows()
        for column in list_:
            last = projects.last_of_base(list_, column)
            after = [count_label(cards[column.key]), self._trash(column, last)]
            element = self.add_row(column.key, self._tools(column, last), self._name(column), after)
            element.setData(KEY, column.key)
            element.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled)

    def _name(self, column: projects.Column) -> QLineEdit:
        name = QLineEdit(projects.column_name(column))
        project, key = self.project, column.key
        name.editingFinished.connect(lambda: self._rename(project, key, name))
        return name

    def _tools(self, column: projects.Column, last: bool) -> list[QWidget]:
        """Grip and category; the last column of a category keeps it."""
        grip = QLabel()
        grip.setPixmap(style.icon(GRIP, tone()).pixmap(QSize(16, 16)))
        grip.setCursor(Qt.CursorShape.OpenHandCursor)
        grip.setToolTip(language.tr("Drag to change the order"))
        kind = QToolButton()
        kind.setIcon(QIcon(state_sign(column.base, tone())))
        kind.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        project = self.project
        kind.setMenu(
            choice_menu(
                QMenu(kind),
                state_options(),
                column.base,
                lambda b: self.write(lambda: tasks.rebase_column(self.recordings, project, column.key, str(b))),
            )
        )
        kind.setToolTip(language.tr("Category: {base}", base=self._base(column)))
        if last:
            self._keep(kind, column)
        return [grip, kind]

    def _trash(self, column: projects.Column, last: bool) -> QToolButton:
        trash = trash_button(language.tr("Delete column"), lambda: self._delete(column.key))
        if last:
            self._keep(trash, column)
        return trash

    @staticmethod
    def _base(column: projects.Column) -> str:
        return language.tr(projects.BASES[column.base])

    def _keep(self, button: QToolButton, column: projects.Column) -> None:
        button.setEnabled(False)
        button.setToolTip(language.tr("The last “{base}” column: the board needs one", base=self._base(column)))

    def _move(self, key: str, place: int) -> None:
        keys = [c.key for c in self._columns()]
        if key in keys:
            keys.insert(place, keys.pop(keys.index(key)))
        self.write(lambda: projects.order_columns(self.recordings, self.project, keys))

    def _rename(self, project: str, key: str, field: QLineEdit) -> None:
        def stored() -> str | None:
            column = next((c for c in projects.columns(self.recordings, project) if c.key == key), None)
            return None if column is None else projects.column_name(column)

        self.rename(field, lambda text: projects.rename_column(self.recordings, project, key, text), stored)

    def _add(self) -> None:
        """A new "In progress" column after the last such one, with a free name selected for typing over."""
        list_ = self._columns()
        name = free_name(
            language.tr("New column"),
            lambda k: language.tr("New column {n}", n=k),
            map(projects.column_name, list_),
        )
        after = [c for c in list_ if c.base == tasks.IN_PROGRESS][-1]
        added: list[projects.Column] = []
        if self.write(lambda: added.append(projects.add_column(self.recordings, self.project, name, after.key)), False):
            self.refresh()
            self.focus(added[0].key)

    def _delete(self, key: str) -> None:
        list_ = self._columns()
        column = next((c for c in list_ if c.key == key), None)
        if column is None:
            return
        others = [c for c in list_ if c.key != key]
        cards = sum(z.column == key for z in tasks.tasks(self.recordings, self.project))
        if cards:
            target = ask_target(self, column, others, cards)
        elif style.ask(
            self,
            language.tr("Delete column"),
            language.tr("Delete column “{name}”?", name=projects.column_name(column)),
        ):
            target = next(c for c in others if c.base == column.base)
        else:
            target = None
        if target is not None:
            self.write(lambda: tasks.delete_column(self.recordings, self.project, key, target.key))
