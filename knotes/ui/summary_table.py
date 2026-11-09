# SPDX-License-Identifier: GPL-3.0-only
"""The summary table by meeting: costs and models of each meeting in the period, shown only here, not in the meeting
itself, and the key spending outside meetings as the last row, so the table adds up to "Spent"."""

import pathlib

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

from knotes import summary
from knotes.language import tr, tr_key
from knotes.summary_report import OUTSIDE, OUTSIDE_HINT, clock, or_dash
from knotes.ui import style

OUTSIDE_ROW = Qt.ItemDataRole.UserRole + 1  # marks the table row of spending outside meetings


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

    def set_(self, lines: list[summary.SummaryRow], outside: float = 0.0) -> None:
        """Rows by meeting, newest on top unless the user sorted by another column; hovering shows what the money
        went on, which model and what share. outside is key spending outside meetings: a row at the end so the
        table adds up to "Spent"."""
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
                Number(or_dash(speech), speech),
                Number(or_dash(minutes), minutes),
                Number(or_dash(result.cost), result.cost),
                QTableWidgetItem(", ".join(name for _, name, _, _ in summary.models(result) if name)),
            ]
            tooltip = style.plain_tooltip("\n".join(summary.arrangement(result)))
            for column, cell in enumerate(cells):
                cell.setToolTip(tooltip)
                self.setItem(row, column, cell)
        self.set_outside(outside)
        self.setSortingEnabled(True)
        self.sortItems(by, Qt.SortOrder.DescendingOrder if order is None else order)

    def set_outside(self, outside: float) -> None:
        """The row of key spending outside meetings, added, changed or removed; the meeting rows stay."""
        sorting = self.isSortingEnabled()
        self.setSortingEnabled(False)
        row = next((n for n in range(self.rowCount()) if self.item(n, 0).data(OUTSIDE_ROW)), None)
        if row is not None and not outside:
            self.removeRow(row)
        elif outside:
            if row is None:
                row = self.rowCount()
                self.insertRow(row)
            cells = [QTableWidgetItem(tr(OUTSIDE)), *(Number("-", 0) for _ in range(4))]
            cells += [Number(or_dash(outside), outside), QTableWidgetItem("")]
            cells[0].setData(OUTSIDE_ROW, True)
            for column, cell in enumerate(cells):
                cell.setToolTip(style.plain_tooltip(tr(OUTSIDE_HINT)))
                self.setItem(row, column, cell)
        self.setSortingEnabled(sorting)
