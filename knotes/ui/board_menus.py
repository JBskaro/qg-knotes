# SPDX-License-Identifier: GPL-3.0-only
"""Menus of the task board: a menu rebuilt on every show, a choice with a check mark on the current item, the due
date menu and its calendar."""

import datetime
from collections.abc import Callable, Sequence

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QActionGroup, QColor, QIcon, QPalette, QTextCharFormat
from PySide6.QtWidgets import QApplication, QCalendarWidget, QMenu, QWidgetAction

from knotes import language
from knotes.ui import style


def cleaned(menu: QMenu) -> QMenu:
    """A menu rebuilt on every show: the previous items and their groups are removed."""
    menu.clear()
    for group in menu.findChildren(QActionGroup):
        group.deleteLater()
    return menu


def choice_menu(
    menu: QMenu,
    variants: Sequence[tuple[object, str, QIcon | None]],
    current_value: object,
    select: Callable[[object], object],
) -> QMenu:
    """Items with a check mark on the current one: status, priority, assignee, board filters."""
    group = QActionGroup(menu)
    for value, row_label, icon in variants:
        item = menu.addAction(icon or QIcon(), style.menu_text(row_label), lambda z=value: select(z))
        item.setCheckable(True)
        item.setChecked(value == current_value)
        group.addAction(item)
    return menu


def due_calendar(due: datetime.date | None, select: Callable[[datetime.date], object]) -> QCalendarWidget:
    """A calm calendar: weekends in the same color, no week numbers. Colors come from the program, not from the menu
    it sits in: a Windows 11 menu has its own dark background."""
    calendar = QCalendarWidget()
    calendar.setPalette(QApplication.palette())
    calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
    calendar.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
    palette_ = QApplication.palette()
    regular, header = QTextCharFormat(), QTextCharFormat()
    for format_, color in ((regular, palette_.color(QPalette.ColorRole.Text)), (header, QColor(style.text_tones()[1]))):
        format_.setForeground(color)
        format_.setBackground(palette_.color(QPalette.ColorRole.Base))  # without a background the cells are transparent
    for day in Qt.DayOfWeek:
        calendar.setWeekdayTextFormat(day, regular)
    calendar.setHeaderTextFormat(header)
    if due:
        calendar.setSelectedDate(QDate(due.year, due.month, due.day))
    calendar.clicked.connect(lambda day: select(day.toPython()))
    return calendar


def due_menu(
    menu: QMenu,
    due: datetime.date | None,
    select: Callable[[datetime.date | None], object],
    day: Callable[[], object] | None = None,
) -> QMenu:
    """Today, tomorrow, in a week, a custom date and "Remove due date". A custom date is picked in a calendar right
    in the menu or with the "Pick a date…" item (day: what to do then)."""
    today = datetime.date.today()
    for row_label, days_count in (
        (language.tr("Today"), 0),
        (language.tr("Tomorrow"), 1),
        (language.tr("In a week"), 7),
    ):
        menu.addAction(row_label, lambda d=days_count: select(today + datetime.timedelta(days=d)))
    menu.addSeparator()
    if day is not None:
        menu.addAction(language.tr("Pick a date…"), day)
    else:

        def selected(date: datetime.date) -> None:
            menu.close()
            select(date)

        action = QWidgetAction(menu)
        action.setDefaultWidget(due_calendar(due, selected))
        menu.addAction(action)
    menu.addSeparator()
    menu.addAction(language.tr("Remove due date"), lambda: select(None))
    return menu
