# SPDX-License-Identifier: GPL-3.0-only
"""The "Summary" window: filters in one row (period and project), below them spent, meetings and hours, costs and
meeting hours by day, week or month (by the period length), projects and models, the key balance.

What is shown is counted in knotes/summary_report.py; the window draws it: the charts (summary_charts), the filters
(summary_filters) and the table by meeting (summary_table) are modules of their own.
"""

import datetime
import logging
import pathlib
from collections.abc import Callable, Sequence

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget

from knotes import catalog, failures, providers, spending, summary_report
from knotes.language import tr
from knotes.summary_report import Figure, Line, clock, dollars, number
from knotes.ui import style
from knotes.ui.background import run_in_background
from knotes.ui.summary_charts import Columns, Strip, gray, kind_color, legend
from knotes.ui.summary_filters import Filters
from knotes.ui.summary_table import MeetingsTable

log = logging.getLogger(__name__)


class Tile(QFrame):
    """One summary figure: caption, value, hint and, if needed, a strip."""

    def __init__(self, row_label: str, with_strip: bool = False):
        super().__init__()
        self.setObjectName("плитка")
        header = QLabel(row_label)
        header.setObjectName("второй_текст")
        self.value, self.hint = QLabel("…"), QLabel()
        for caption in (self.value, self.hint):  # the hint may hold the service's refusal text
            caption.setTextFormat(Qt.TextFormat.PlainText)
        self.value.setStyleSheet("font-size: 20px; font-weight: 600;")
        self.hint.setObjectName("тихий_текст")
        self.hint.setWordWrap(True)
        self.strip = QProgressBar(self)  # shown before the layout takes it: without a parent it would flash
        self.strip.setObjectName("ход")
        self.strip.setTextVisible(False)
        self.strip.setFixedHeight(6)
        self.strip.setVisible(with_strip)
        column = QVBoxLayout(self)
        column.setContentsMargins(12, 10, 12, 10)
        column.setSpacing(2)
        for widget in (header, self.value, self.strip, self.hint):
            column.addWidget(widget)

    def set_(self, figure: Figure) -> None:
        self.value.setText(figure.value)
        self.hint.setText(figure.hint)
        if figure.share is not None:
            self.strip.setValue(round(figure.share * 100))


WAITING = object()  # the balance has not come yet; None is the answer "this provider does not report it"


class SummaryWindow(QWidget):
    """Summary of the recordings folder; the key balance is requested from the provider in the background on every
    opening. Signal open_meeting(folder) on a double click on a meeting in the table."""

    open_meeting = Signal(pathlib.Path)
    _balance_arrived = Signal(object)

    def __init__(
        self,
        recordings: pathlib.Path,
        fetch_balance: Callable[[], providers.Balance | None] | None,
        provider: str,
        projects: tuple[Sequence[str], dict[str, dict]] = ((), {}),
        project: str | None = None,
    ):
        """fetch_balance is None when speech is recognized on this computer: there is no key. projects are the
        created projects with their colors and icons; project is the one selected at once (None means all)."""
        super().__init__()
        self.recordings, self._fetch_balance, self._provider = recordings, fetch_balance, provider
        self._balance: object = WAITING
        self._balance_at = datetime.datetime.now()  # when the balance came from the service
        self._report: summary_report.Report | None = None  # counted for the filters
        self._balance_arrived.connect(self._show_balance)
        # read once: the filters and the balance count from memory, not from the folder again
        self._all_meetings = catalog.list_(recordings)
        self._all_expenses = self._read_spending()
        known, project_looks = projects
        self._project_looks = project_looks
        self.filters = Filters(catalog.projects(self._all_meetings, list(known)), project_looks, project)
        self.filters.changed.connect(self.display)
        self.refreshed = QLabel()
        self.refreshed.setObjectName("тихий_текст")
        self.tiles = [
            Tile(tr("Spent")),
            Tile(tr("Встреч")),
            Tile(tr("Meeting hours")),
            Tile(tr("Key balance now"), True),
        ]  # the balance does not depend on the filters
        self.expenses = Columns(catalog.money, lambda z: dollars(number(z)))
        self.clock = Columns(lambda z: clock(z * 3600), lambda z: tr("{hours} h", hours=number(z)))
        self._headings = [self._heading(""), self._heading("")]
        self.legend = QLabel()
        self.legend.setObjectName("второй_текст")
        self.legend.setTextFormat(Qt.TextFormat.RichText)
        self._projects, self._models = QGridLayout(), QGridLayout()
        self.by_meetings = MeetingsTable()
        self.by_meetings.open_view.connect(self.open_meeting)
        self._build()
        self._paint()

    def _read_spending(self) -> list[spending.Expense]:
        """The spending log; one that cannot be read leaves the meeting prices, and "Spent" says why."""
        self._spending_error = ""
        try:
            return spending.list_(self.recordings, self._all_meetings)
        except (OSError, ValueError) as error:
            log.warning("the spending log was not read: %s", error)
            self._spending_error = tr("The spending log was not read: {reason}", reason=failures.text(error))
            return spending.from_meetings(self._all_meetings)

    def _build(self) -> None:
        top = QHBoxLayout()
        top.addWidget(self.filters, 1)
        top.addWidget(self.refreshed)
        tiles = QHBoxLayout()
        for tile in self.tiles:
            tiles.addWidget(tile, 1)
        charts, splits = QHBoxLayout(), QHBoxLayout()
        for row, parts in (
            (charts, zip(self._headings, (self.expenses, self.clock), (self.legend, None), strict=True)),
            (
                splits,
                (
                    (self._heading(tr("By project")), self._projects, None),
                    (self._heading(tr("By model")), self._models, None),
                ),
            ),
        ):
            row.setSpacing(20)
            for heading, body, right_ in parts:
                row.addLayout(self._column(heading, body, right_), 1)
        column = QVBoxLayout(self)
        column.setContentsMargins(16, 12, 16, 16)
        column.setSpacing(12)
        for part in (top, tiles, charts, splits):
            column.addLayout(part)
        column.addWidget(self._heading(tr("By meeting")))
        column.addWidget(self.by_meetings, 1)

    @staticmethod
    def _column(heading: QLabel, body: QWidget | QGridLayout, right_: QLabel | None) -> QVBoxLayout:
        """A heading (the chart legend to its right) and below it a chart or breakdown rows."""
        header = QHBoxLayout()
        header.addWidget(heading)
        header.addStretch()
        if right_:
            header.addWidget(right_)
        col = QVBoxLayout()
        col.addLayout(header)
        if isinstance(body, QGridLayout):
            body.setColumnStretch(1, 1)
            body.setHorizontalSpacing(12)
            col.addLayout(body)
            col.addStretch()  # rows right under the heading, level with the column next to it
        else:
            col.addWidget(body)
        return col

    @staticmethod
    def _heading(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-weight: 600;")
        return label

    def repaint_theme(self) -> None:
        self._paint()
        if self._report is not None:
            self.display()

    def _paint(self) -> None:
        background_, frame_box = (
            ("rgba(255,255,255,0.04)", "rgba(255,255,255,0.12)") if style.dark() else ("#FFFFFF", "rgba(0,0,0,0.10)")
        )
        secondary = style.text_tones()[0]
        self.setStyleSheet(
            style.window_style()
            + f"QFrame#плитка {{ background: {background_}; border: 1px solid {frame_box}; border-radius: 8px; }}"
            f"QTableWidget#по_встречам {{ background: {background_}; border: 1px solid {frame_box}; "
            f"border-radius: 8px; }}"
            f"QTableWidget#по_встречам::item {{ padding: 4px 8px; border-bottom: 1px solid {frame_box}; }}"
            f"QTableWidget#по_встречам QHeaderView::section {{ background: transparent; border: none; "
            f"border-bottom: 1px solid {frame_box}; padding: 6px 8px; color: {secondary}; }}"
        )

    def refresh_balance(self) -> None:
        if self._fetch_balance is None:
            self.tiles[3].set_(Figure("-", tr("Speech is recognized on this computer, free of charge"), 0))
            return
        self.tiles[3].set_(Figure("…", tr("Asking {provider}", provider=self._provider), 0))
        run_in_background(QThreadPool.globalInstance(), self._fetch_balance, self._balance_arrived.emit)

    def display(self) -> None:
        """Figures, charts and projects for the selected period and project, from the meetings and spending read when
        the window opened."""
        selection = self.filters.selection()
        self._report = report = summary_report.report(
            self._all_meetings, self._all_expenses, selection, datetime.date.today()
        )
        self.window().setWindowTitle(summary_report.title(selection.project, style.NAME))
        self.tiles[1].set_(report.meetings)
        self.tiles[2].set_(report.hours)
        for heading, text in zip(self._headings, report.headings, strict=True):
            heading.setText(text)
        self.legend.setText(legend())
        self.expenses.set_(report.costs, report.current, report.cost_tips)
        self.clock.set_(report.hours_bars, report.current)
        self.by_meetings.set_(report.rows)
        self._show_spending()

    def _show_spending(self) -> None:
        """What the key balance changes: "Spent", key spending outside meetings in the breakdowns and the table, and
        the balance tile. The rest of the summary is not counted again when the balance arrives."""
        if self._report is None:
            return
        balance = self._balance if isinstance(self._balance, providers.Balance) else None
        spent = summary_report.spending(self._report, balance, self._provider, self._spending_error)
        self.tiles[0].set_(spent.spent)
        self._show_lines(self._projects, spent.projects, tr("No meetings in this period"))
        self._show_lines(self._models, spent.models, tr("No costs in this period"))
        self.by_meetings.set_outside(spent.outside)
        if self._balance is not WAITING:
            self._balance_tile()

    def _show_lines(self, grid_layout: QGridLayout, lines: list[Line], empty: str) -> None:
        """Breakdown rows: name, share strip, figure on the right, the tooltip on the whole row; without rows, empty
        says so. The old rows go first."""
        style.empty(grid_layout)
        if not lines:
            caption = QLabel(empty)
            caption.setObjectName("тихий_текст")
            grid_layout.addWidget(caption, 0, 0, 1, 3)
            return
        for row, line in enumerate(lines):
            name, figure = style.caption(line.name), QLabel(line.figure)
            name.setObjectName("второй_текст" if line.quiet else "")
            name.setMinimumWidth(150)
            figure.setObjectName("второй_текст")
            for column, widget in enumerate((name, Strip(line.share, self._strip_color(line)), figure)):
                widget.setToolTip(style.plain_tooltip(line.tooltip))  # project and model names as text
                grid_layout.addWidget(
                    widget, row, column, Qt.AlignmentFlag.AlignRight if column == 2 else Qt.AlignmentFlag(0)
                )

    def _strip_color(self, line: Line) -> str:
        """Gray for a quiet row, the project's own color if it has one, else the color of the bar part."""
        if line.quiet:
            return gray()
        color = self._project_looks.get(line.project, {}).get("цвет") if line.project else None
        return kind_color(line.kind) if color is None else style.participant_color(color)[0]

    def _show_balance(self, balance: object) -> None:
        """The balance arrived: the balance tile and the spending it changes, "Spent" may become the service
        account."""
        self._balance, self._balance_at = balance, datetime.datetime.now()
        self._show_spending()

    def _balance_tile(self) -> None:
        self.refreshed.setText(
            tr("{provider}, updated at {time}", provider=self._provider, time=f"{self._balance_at:%H:%M}")
        )
        self.tiles[3].set_(summary_report.balance_figure(self._balance, self._provider, self._all_meetings))
