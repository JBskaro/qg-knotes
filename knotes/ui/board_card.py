# SPDX-License-Identifier: GPL-3.0-only
"""A card of the task board: priority, what to do, labels, assignee, due date, checklist and source; what it is drawn
with besides the task. The source is a link to the meeting, date and minute the task came from."""

import dataclasses
import datetime
import html
import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from knotes import language, tasks, transcript
from knotes.ui import style
from knotes.ui.board_chips import ChipRow, chip
from knotes.ui.board_icons import priority_sign, tone
from knotes.ui.board_style import OPENED

CALENDAR, LIST = "", ""  # Windows font icons: calendar and to-do list
ICON_FONT = "font-family: 'Segoe Fluent Icons', 'Segoe MDL2 Assets'; font-size: 11px;"
DAYS = language.forms("день", "дня", "дней")


def _with_icon(icon: str, text: str) -> str:
    return f'<span style="{ICON_FONT}">{icon}</span>&nbsp;{html.escape(text)}'


def _date(due: datetime.date, today: datetime.date) -> str:
    """16.10 in this year, 16.10.2025 in another one."""
    return f"{due:%d.%m}" if due.year == today.year else f"{due:%d.%m.%Y}"


def _due_caption(task: tasks.Task, today: datetime.date) -> QLabel:
    """An overdue date in red, a date within the next week in amber, otherwise calm; details in the tooltip."""
    caption = QLabel(_with_icon(CALENDAR, _date(task.due, today)))
    if task.overdue(today):
        caption.setObjectName("ошибка_текст")
        caption.setToolTip(language.tr("Overdue by {days}", days=language.count((today - task.due).days, DAYS)))
    else:
        caption.setObjectName("скоро_текст" if task.soon(today) else "второй_текст")
        caption.setToolTip(language.tr("Due {date}", date=f"{task.due:%d.%m.%Y}"))
    return caption


def source_link(task: tasks.Task) -> str:
    """Meeting, date and minute as a calm link so it does not outshine the task itself; for an own task, when it
    was added. The title comes with the card: a thousand cards do not read a thousand meetings."""
    o_ = task.origin
    if o_.folder is None:
        return html.escape(language.tr("Added manually {date}", date=f"{o_.date:%d.%m.%Y}"))
    time = f", {transcript.time(o_.second)}" if o_.second is not None else ""
    return (
        f"<a href='#' style='color:{style.text_tones()[0]}; text-decoration:none'>"
        f"{html.escape(tasks.source_title(o_))}, {o_.date:%d.%m.%Y}{time}</a>"
    )


def _source(task: tasks.Task) -> str:
    if task.origin.folder is None:
        return source_link(task)
    return language.tr("Source · {url}", url=source_link(task))


@dataclasses.dataclass(frozen=True)
class CardLook:
    """What a card needs besides the task itself: today's date, label colors, which card is open in the panel and, on
    the board of all projects, the names of the project columns the cards are in (by card key)."""

    today: datetime.date
    colors: dict[str, int]
    opened: str = ""
    marks: dict[str, str] = dataclasses.field(default_factory=dict)


class TaskCard(QFrame):
    """Priority, what to do, labels, assignee, due date, checklist and source; a question or decision has a dashed
    border."""

    open_place = Signal(object, float)  # a click on the source: meeting folder and source second

    def __init__(self, task: tasks.Task, look: CardLook):
        super().__init__()
        self.task = task
        self.setObjectName("карточка_задачи" if task.kind == tasks.TASK else "вопрос_доски")
        self.setProperty(OPENED, task.key == look.opened)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        column = QVBoxLayout(self)
        column.setContentsMargins(10, 8, 10, 8)
        column.setSpacing(6)
        column.addLayout(self._heading())
        if task.labels:
            labels = ChipRow()
            for name in task.labels:
                labels.addWidget(chip(name, look.colors.get(name, 0)))
            column.addLayout(labels)
        if info := self._info(look.today, look.marks.get(task.key, "")):
            row = QHBoxLayout()
            row.setSpacing(10)
            for widget in info:
                row.addWidget(widget)
            row.addStretch()
            column.addLayout(row)
        origin = QLabel(_source(task))
        origin.setObjectName("тихий_текст")
        origin.setWordWrap(True)
        origin.linkActivated.connect(
            lambda _link: self.open_place.emit(task.origin.folder, float(task.origin.second or 0))
        )
        column.addWidget(origin)

    def _heading(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        if self.task.priority:
            sign = QLabel()
            sign.setPixmap(priority_sign(self.task.priority, tone()))
            sign.setToolTip(language.tr(tasks.PRIORITIES.get(self.task.priority, self.task.priority)))
            sign.setContentsMargins(0, 2, 0, 0)
            row.addWidget(sign, 0, Qt.AlignmentFlag.AlignTop)
        self.title = QLabel(self.task.text)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        if self.task.status == tasks.DONE:
            self.title.setObjectName("сделано")
        row.addWidget(self.title, 1)
        return row

    def light_up(self, found: re.Pattern | None) -> None:
        """The words the board search found, on a pale background; the same text, so the card keeps its height."""
        pale = style.HIGHLIGHT[style.dark()][0]
        marked, count = style.highlighted(self.task.text, found, lambda _n: pale) if found else ("", 0)
        lit = self.title.textFormat() == Qt.TextFormat.RichText
        if count:
            self.title.setTextFormat(Qt.TextFormat.RichText)
            self.title.setText(marked)
        elif lit:
            self.title.setTextFormat(Qt.TextFormat.PlainText)
            self.title.setText(self.task.text)

    def _info(self, today: datetime.date, column: str) -> list[QWidget]:
        task, result = self.task, []
        if column:
            mark = style.Elided(column, language.tr("Column on the project board"))
            mark.setObjectName("тихий_текст")
            result.append(mark)
        if task.kind != tasks.TASK:  # not an assignee but who answers or confirms: "Anna will check with the warehouse"
            who = style.Elided(" · ".join(ch for ch in (language.tr(task.kind), task.who) if ch))
            who.setObjectName("второй_текст")
            result.append(who)
        elif task.who:
            result.append(chip(task.who, None))
        if task.due:
            result.append(_due_caption(task, today))
        if task.checklist:
            done_count, total = sum(p.is_done for p in task.checklist), len(task.checklist)
            list_ = QLabel(_with_icon(LIST, f"{done_count}/{total}"))
            list_.setObjectName("второй_текст")
            list_.setToolTip(language.tr("Checklist: {done_count} of {total} done", done_count=done_count, total=total))
            result.append(list_)
        return result
