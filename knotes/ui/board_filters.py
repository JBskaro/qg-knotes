# SPDX-License-Identifier: GPL-3.0-only
"""Filters of the task board: assignee, label, priority and due date, each a button with a menu; what they let
through together with the search and the archive."""

from collections.abc import Sequence

from PySide6.QtCore import Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QPushButton

from knotes import language, tasks
from knotes.ui import style
from knotes.ui.board_icons import color_sign, priority_options
from knotes.ui.board_menus import choice_menu, cleaned
from knotes.ui.board_style import SET


class FilterButton(QPushButton):
    """A board filter as a button with a menu: without a filter the button shows what it filters ("Assignee"), with
    one it shows the choice and is highlighted. The selected(value) signal comes after a choice; None means no
    filter."""

    selected = Signal(object)

    def __init__(self, title: str, all_: str):
        super().__init__()
        self.setObjectName("отбор")
        self._title, self._all_caption = title, all_
        self.value: object = None
        self._variants: list[tuple[object, str, QIcon | None]] = []
        self.setMenu(QMenu(self))
        self.menu().aboutToShow.connect(
            lambda: choice_menu(
                cleaned(self.menu()), [(None, self._all_caption, None), *self._variants], self.value, self.select
            )
        )
        self._show_choice()

    def set_variants(self, variants: Sequence[tuple[object, str, QIcon | None]]) -> None:
        """The choice stays if it is still among the variants."""
        self._variants = list(variants)
        if all(value != self.value for value, *_ in self._variants):
            self.value = None
        self._show_choice()

    def select(self, value: object) -> None:
        self.value = value
        self._show_choice()
        self.selected.emit(value)

    def _show_choice(self) -> None:
        self.setText(
            style.menu_text(
                next(
                    (
                        row_label
                        for value, row_label, _ in self._variants
                        if value == self.value and self.value is not None
                    ),
                    self._title,
                )
            )
        )
        self.setProperty(SET, self.value is not None)
        style.restyle(self)


def filter_buttons() -> dict[str, FilterButton]:
    """The filters by the names the board knows them by."""
    return {
        "кто": FilterButton(language.tr("Assignee"), language.tr("All assignees")),
        "метка": FilterButton(language.tr("Label"), language.tr("All labels")),
        "приоритет": FilterButton(language.tr("Priority"), language.tr("Any priority")),
        "срок": FilterButton(language.tr("Due date"), language.tr("Any due date")),
    }


def set_variants(filters: dict[str, FilterButton], cards: list[tasks.Task], labels: list[tasks.Label]) -> None:
    """The choices of the filters for the cards and labels on the board."""
    filters["кто"].set_variants([(i, i, None) for i in tasks.assignees(cards)])
    filters["метка"].set_variants([(m.name, m.name, QIcon(color_sign(m.color))) for m in labels])
    filters["приоритет"].set_variants(priority_options())
    filters["срок"].set_variants([(s, language.tr(p), None) for s, p in tasks.DUES.items()])


def chosen(filters: dict[str, FilterButton], search: str, archive: bool) -> tasks.Filter:
    """What the filters, the search text and the archive button let through."""
    return tasks.Filter(
        filters["кто"].value,
        filters["метка"].value,
        filters["приоритет"].value,
        filters["срок"].value,
        search,
        archive,
    )
