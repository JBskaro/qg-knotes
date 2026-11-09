# SPDX-License-Identifier: GPL-3.0-only
"""Summary filters in one row above the figures and charts: "‹ October 2026 ›" (ready-made periods in a list, custom
dates as the last item) and the project."""

import datetime
from collections.abc import Sequence

from PySide6.QtCore import QDate, QLocale, QTimer, Signal
from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import QDateEdit, QHBoxLayout, QMenu, QPushButton, QWidget

from knotes import catalog, language, summary
from knotes.language import tr, tr_key
from knotes.summary_report import Selection
from knotes.ui import style

BACK, FORWARD = "", ""  # arrows from the Windows icon font
DATES_PAUSE_MS = 300  # typing a date: count once it stops changing


class Filters(QWidget):
    """Signal changed() after any change, for a typed date once it stops changing: everything below is
    recalculated."""

    changed = Signal()
    PERIODS = (
        (summary.MONTH, 0, tr_key("Этот месяц")),
        (summary.MONTH, 1, tr_key("Прошлый месяц")),
        (summary.THREE_MONTHS, 0, tr_key("Последние 3 месяца")),
        (summary.YEAR, 0, tr_key("Этот год")),
        (summary.ALL, 0, tr_key("Все время")),
        (summary.CUSTOM, 0, tr_key("Свои даты…")),
    )

    def __init__(self, projects: Sequence[str], project_looks: dict[str, dict], project: str | None):
        super().__init__()
        self.kind, self.shift, self.project = summary.MONTH, 0, project
        self._dates_later = QTimer(self, singleShot=True, interval=DATES_PAUSE_MS)
        self._dates_later.timeout.connect(self.changed.emit)
        today = datetime.date.today()
        self.s, self.by = self._date(today.replace(day=1), tr("Start date")), self._date(today, tr("End date"))
        self._back = style.icon_button(BACK, tr("Previous period"), lambda: self._step(1))
        self._forward = style.icon_button(FORWARD, tr("Next period"), lambda: self._step(-1))
        self._period, self._project = QPushButton(), QPushButton()
        self._period.setMenu(self._period_menu())
        self._project.setMenu(style.projects_menu(self, projects, project_looks, project, self._pick_project))
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        for widget in (self._back, self._period, self._forward, self.s, self.by):
            row.addWidget(widget)
        row.addSpacing(10)
        row.addWidget(self._project)
        row.addStretch()
        self._label_items()

    def _date(self, date: datetime.date, tooltip: str) -> QDateEdit:
        """Date field with a calendar on an arrow click; months and weekdays in the calendar are in the UI language."""
        field = QDateEdit(QDate(date.year, date.month, date.day))
        if language.current() != "ru":
            field.setLocale(QLocale(language.current()))
        field.setCalendarPopup(True)
        field.setDisplayFormat("dd.MM.yyyy")
        field.setToolTip(tooltip)
        field.dateChanged.connect(lambda _date: self._dates_later.start())
        return field

    def _period_menu(self) -> QMenu:
        """Ready-made periods; the current one is checked (after the arrows none may be)."""
        menu, group = QMenu(self), QActionGroup(self)
        group.setExclusionPolicy(QActionGroup.ExclusionPolicy.ExclusiveOptional)
        self._period_actions = []
        for kind, shift, row_label in self.PERIODS:
            if kind == summary.CUSTOM:
                menu.addSeparator()
            item = menu.addAction(tr(row_label), lambda v=kind, s=shift: self._pick_period(v, s))
            item.setCheckable(True)
            group.addAction(item)
            self._period_actions.append((kind, shift, item))
        return menu

    def _pick_period(self, kind: str, shift: int = 0) -> None:
        self.kind, self.shift = kind, shift
        self._label_items()
        self.changed.emit()

    def _pick_project(self, project: str | None) -> None:
        self.project = project
        self._label_items()
        self.changed.emit()

    def _step(self, back: int) -> None:
        """Arrows: to the previous or next period of the same kind; custom dates shift by their own length."""
        if self.kind == summary.CUSTOM:
            s, by = summary.period(summary.CUSTOM, datetime.date.today(), self.dates(), back)
            for field, date in ((self.s, s), (self.by, by - datetime.timedelta(days=1))):
                field.blockSignals(True)
                field.setDate(QDate(date.year, date.month, date.day))
                field.blockSignals(False)
        else:
            self.shift = max(0, self.shift + back)
        self._label_items()
        self.changed.emit()

    def _label_items(self) -> None:
        custom_one = self.kind == summary.CUSTOM
        self._period.setText(
            tr("Custom dates") if custom_one else summary.row_label(self.kind, datetime.date.today(), shift=self.shift)
        )
        for field in (self.s, self.by):
            field.setVisible(custom_one)
        self._back.setEnabled(self.kind != summary.ALL)
        self._forward.setEnabled(custom_one or self.shift > 0)
        self._project.setText(tr("All projects") if self.project is None else self.project or tr(catalog.NO_PROJECT))
        for kind, shift, item in self._period_actions:
            item.setChecked(kind == self.kind and (shift == self.shift or custom_one))

    def dates(self) -> tuple[datetime.date, datetime.date]:
        return self.s.date().toPython(), self.by.date().toPython()

    def selection(self) -> Selection:
        """What is selected, for counting the summary."""
        return Selection(self.kind, self.shift, self.dates(), self.project)
