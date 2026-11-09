# SPDX-License-Identifier: GPL-3.0-only
"""A tab of the board settings: rows written at once (the columns of a project, the labels), a button that adds one
and a note under them. A refused or failed change is shown as a message; every written one emits changed."""

import itertools
from collections.abc import Callable, Iterable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from knotes import language
from knotes.ui import style
from knotes.ui.board_writes import safely

TRASH = ""  # icon font: "Delete"
CARDS = language.forms("карточка", "карточки", "карточек")


def free_name(first: str, numbered: Callable[[int], str], taken: Iterable[str]) -> str:
    """first, else numbered(2), numbered(3)…: the first one not taken, in any case."""
    busy = {name.casefold() for name in taken}
    return next(n for n in itertools.chain([first], map(numbered, itertools.count(2))) if n.casefold() not in busy)


def dialog_button(text: str, action: Callable[[], object]) -> QPushButton:
    """A button of the settings window that Enter in a name field does not press."""
    button = QPushButton(text)
    button.setAutoDefault(False)
    button.clicked.connect(action)
    return button


def count_label(cards: int) -> QLabel:
    label = QLabel(language.count(cards, CARDS))
    label.setObjectName("второй_текст")
    return label


def trash_button(tooltip: str, action: Callable[[], object]) -> QToolButton:
    return style.icon_button(TRASH, tooltip, action)


class SettingsPage(QWidget):
    """names: the name field of every row by its key; refresh() builds the rows again, _add() adds one."""

    changed = Signal()

    def __init__(self, list_: QListWidget, add_text: str, note_text: str, top: QWidget | None = None):
        super().__init__()
        self.list_ = list_
        self.names: dict[str, QLineEdit] = {}
        self._rebuild = QTimer(self, singleShot=True, interval=0)  # not from inside a signal of a row being replaced
        self._rebuild.timeout.connect(self.refresh)
        add = dialog_button(add_text, self._add)
        add.setObjectName("назад")
        note = QLabel(note_text)
        note.setObjectName("второй_текст")
        note.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if top is not None:
            layout.addWidget(top)
        layout.addWidget(style.layer(list_, padding=4), 1)
        layout.addWidget(add, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(note)

    def refresh(self) -> None:
        raise NotImplementedError

    def _add(self) -> None:
        raise NotImplementedError

    def clear_rows(self) -> None:
        self.list_.clear()
        self.names.clear()

    def add_row(self, key: str, before: list[QWidget], name: QLineEdit, after: list[QWidget]) -> QListWidgetItem:
        """A row: the widgets before the name, the name stretched, the widgets after it."""
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(4, 2, 4, 2)
        line.setSpacing(6)
        for widget in before:
            line.addWidget(widget)
        line.addWidget(name, 1)
        for widget in after:
            line.addWidget(widget)
        self.names[key] = name
        element = QListWidgetItem(self.list_)
        element.setFlags(Qt.ItemFlag.ItemIsEnabled)
        element.setSizeHint(row.sizeHint())
        self.list_.setItemWidget(element, row)
        return element

    def write(self, action: Callable[[], object], rebuild: bool = True) -> bool:
        """Writes a change; a refusal or a failure is shown as a message. True if written."""
        written = safely(self, action)
        if written:
            self.changed.emit()
        if rebuild:
            self._rebuild.start()
        return written

    def rename(self, field: QLineEdit, write: Callable[[str], object], stored: Callable[[], str | None]) -> bool:
        """On Enter or leaving the field; a refused name (empty, taken) goes back to the stored one. True if
        written."""
        if not field.isModified():  # the message about a refused name takes the focus and finishes editing again
            return False
        field.setModified(False)
        if self.write(lambda: write(field.text()), rebuild=False):
            return True
        if (name := stored()) is not None:
            field.setText(name)
        return False

    def focus(self, key: str) -> None:
        """The name of a new row, selected for typing over."""
        field = self.names[key]
        field.setFocus()
        field.selectAll()
