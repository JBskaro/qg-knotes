# SPDX-License-Identifier: GPL-3.0-only
"""Costs and models of each meeting in the selected period."""

import pathlib

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

from knotes import summary
from knotes.language import tr, tr_key
from knotes.summary_report import clock, or_dash
from knotes.ui import style


class Number(QTableWidgetItem):
    """A cell sorted by number, not by text: "10,50 $" is greater than "9,00 $"."""

    def __init__(self, text: str, value: float):
        super().__init__(text)
        self.setData(Qt.ItemDataRole.UserRole, value)
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, another: QTableWidgetItem) -> bool:
        return float(self.data(Qt.ItemDataRole.UserRole) or 0) < float(another.data(Qt.ItemDataRole.UserRole) or 0)


class MeetingsTable(QTableWidget):
    """Sorts by any column; a double click on a row emits open_view(meeting folder)."""

    open_view = Signal(pathlib.Path)
    COLUMNS = (
        tr_key("Встреча"),
        tr_key("Дата"),
        tr_key("Длительность"),
        tr_key("Речь"),
        tr_key("Протокол"),
        tr_key("Всего"),
        tr_key("Модели"),
    )

    def __init__(self):
        super().__init__(0, len(self.COLUMNS))
        self.setObjectName("по_встречам")
        self.setHorizontalHeaderLabels([tr(column) for column in self.COLUMNS])
        self.verticalHeader().hide()
        self.setShowGrid(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setMinimumHeight(180)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)  # free space on the right, not as a gap between title and date
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.cellDoubleClicked.connect(self._open_item)

    def _open_item(self, row: int, _column_layout: int) -> None:
        folder = self.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if folder is not None:  # a deleted meeting has nothing to open
            self.open_view.emit(folder)

    def set_(self, lines: list[summary.SummaryRow], currency: str = "USD") -> None:
        """Rows by meeting, newest on top unless the user sorted by another column; hovering shows what the money
        went on, which model and what share."""
        header = self.horizontalHeader()
        by, order = (header.sortIndicatorSection(), header.sortIndicatorOrder()) if self.rowCount() else (1, None)
        self.setSortingEnabled(False)
        self.setRowCount(len(lines))
        for row, line in enumerate(lines):
            result = line.result
            speech, minutes = (how_many for _, how_many in summary.by_kinds(result))
            title = QTableWidgetItem(line.title if line.folder else tr("{title} (deleted)", title=line.title))
            title.setData(Qt.ItemDataRole.UserRole, line.folder)
            cells = [
                title,
                Number(f"{line.date:%d.%m.%Y %H:%M}", line.date.timestamp()),
                Number(clock(line.seconds) if line.seconds else "-", line.seconds),
                Number(or_dash(speech, currency), speech),
                Number(or_dash(minutes, currency), minutes),
                Number(or_dash(result.cost, currency), result.cost),
                QTableWidgetItem(", ".join(name for _, name, _, _ in summary.models(result) if name)),
            ]
            tooltip = style.plain_tooltip("\n".join(summary.arrangement(result, currency)))
            for column, cell in enumerate(cells):
                cell.setToolTip(tooltip)
                self.setItem(row, column, cell)
        self.setSortingEnabled(True)
        self.sortItems(by, Qt.SortOrder.DescendingOrder if order is None else order)
