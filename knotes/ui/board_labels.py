# SPDX-License-Identifier: GPL-3.0-only
"""Recordings folder labels on the task board: the labels menu of a card, the name of a new label, the Labels tab of
the board settings (the name in place, the color, how many cards have it, a new label, delete)."""

import collections
import dataclasses
import pathlib
from collections.abc import Callable

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QMenu,
    QToolButton,
    QWidget,
    QWidgetAction,
)

from knotes import language, tasks
from knotes.ui import style
from knotes.ui.board_icons import color_sign
from knotes.ui.board_settings_page import SettingsPage, count_label, free_name, trash_button

STORED = "stored_name"  # Qt property of a name field: the label name as written, while the field is being edited


def toggled_labels(labels: tuple[str, ...], name: str) -> tuple[str, ...]:
    """The card labels with the label added, or removed if it was there."""
    return tuple(m for m in labels if m != name) if name in labels else (*labels, name)


@dataclasses.dataclass(frozen=True)
class LabelActions:
    toggle: Callable[[str], object]  # label name: add or remove it
    new_one: Callable[[], object]
    configure: Callable[[], object]


def labels_menu(
    menu: QMenu, labels: list[tasks.Label], selected_items: tuple[str, ...], actions: LabelActions
) -> QMenu:
    """Recordings folder labels as check items (whether the card has them), "New label…" and "Manage labels…"."""
    for label in labels:
        item = menu.addAction(
            QIcon(color_sign(label.color)), style.menu_text(label.name), lambda i=label.name: actions.toggle(i)
        )
        item.setCheckable(True)
        item.setChecked(label.name in selected_items)
    if labels:
        menu.addSeparator()
    menu.addAction(language.tr("New label…"), actions.new_one)
    menu.addAction(language.tr("Manage labels…"), actions.configure)
    return menu


def ask_label(parent_: QWidget) -> str:
    """Name of the new label; empty if cancelled."""
    name, yes = QInputDialog.getText(parent_, language.tr("New label"), language.tr("Label name"))
    return name.strip() if yes else ""


class LabelsPage(SettingsPage):
    """The labels are shared by all projects; deleting one takes it off every card. The row keys are the names the
    labels had when the rows were built."""

    def __init__(self, recordings: pathlib.Path):
        super().__init__(
            QListWidget(),
            language.tr("+  New label"),
            language.tr("Labels are shared by all projects. Click the circle to change the color."),
        )
        self.recordings = recordings
        self.list_.setMinimumSize(440, 200)
        self.refresh()

    def refresh(self) -> None:
        cards = collections.Counter(name for z in tasks.tasks(self.recordings) for name in z.labels)
        self.clear_rows()
        for label in tasks.labels(self.recordings):
            name = QLineEdit(label.name)
            name.setProperty(STORED, label.name)
            name.editingFinished.connect(lambda field=name: self._rename(field))
            trash = trash_button(language.tr("Delete label"), lambda _pressed=False, field=name: self._delete(field))
            self.add_row(label.name, [self._color(label, name)], name, [count_label(cards[label.name]), trash])

    def _color(self, label: tasks.Label, field: QLineEdit) -> QToolButton:
        """The circle of the label color; a click opens the colors to choose from."""
        button = QToolButton()
        button.setIcon(QIcon(color_sign(label.color)))
        button.setIconSize(QSize(18, 18))
        button.setAutoRaise(True)
        button.setToolTip(language.tr("Label color"))
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(button)
        colors = QWidget()
        line = QHBoxLayout(colors)
        line.setContentsMargins(6, 4, 6, 4)
        line.setSpacing(2)
        for color in range(tasks.LABEL_COLORS):
            sign = QToolButton()
            sign.setIcon(QIcon(color_sign(color)))
            sign.setIconSize(QSize(18, 18))
            sign.setAutoRaise(True)
            sign.clicked.connect(lambda _pressed=False, c_=color: self._recolor(menu, field, c_))
            line.addWidget(sign)
        action = QWidgetAction(menu)
        action.setDefaultWidget(colors)
        menu.addAction(action)
        button.setMenu(menu)
        return button

    def _recolor(self, menu: QMenu, field: QLineEdit, color: int) -> None:
        menu.close()
        name = str(field.property(STORED))
        self.write(lambda: tasks.recolor_label(self.recordings, name, color))

    def _rename(self, field: QLineEdit) -> None:
        """The new name goes to the cards too; the row keeps it for the next rename, the color and delete."""
        old = str(field.property(STORED))
        if self.rename(field, lambda text: tasks.rename_label(self.recordings, old, text), lambda: old):
            field.setText(field.text().strip())
            field.setProperty(STORED, field.text())

    def _add(self) -> None:
        """A new label with a free name selected for typing over."""
        name = free_name(
            language.tr("New label"),
            lambda k: language.tr("New label {n}", n=k),
            (m.name for m in tasks.labels(self.recordings)),
        )
        if self.write(lambda: tasks.add_label(self.recordings, name), False):
            self.refresh()
            self.focus(name)

    def _delete(self, field: QLineEdit) -> None:
        name = str(field.property(STORED))
        if style.ask(
            self,
            language.tr("Delete label"),
            language.tr("Label “{name}” will be removed from all cards. Delete it?", name=name),
        ):
            self.write(lambda: tasks.delete_label(self.recordings, name))
