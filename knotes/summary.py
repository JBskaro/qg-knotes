# SPDX-License-Identifier: GPL-3.0-only
"""Meetings summary: spending, meetings and hours in a period, chart series and projects."""

import dataclasses
import datetime
import pathlib

from knotes import catalog, providers, spending, language
from knotes.language import tr_key, tr

MONTHS = (
    tr_key("январь"),
    tr_key("февраль"),
    tr_key("март"),
    tr_key("апрель"),
    tr_key("май"),
    tr_key("июнь"),
    tr_key("июль"),
    tr_key("август"),
    tr_key("сентябрь"),
    tr_key("октябрь"),
    tr_key("ноябрь"),
    tr_key("декабрь"),
)
IN_MONTH = (
    tr_key("в январе"),
    tr_key("в феврале"),
    tr_key("в марте"),
    tr_key("в апреле"),
    tr_key("в мае"),
    tr_key("в июне"),
    tr_key("в июле"),
    tr_key("в августе"),
    tr_key("в сентябре"),
    tr_key("в октябре"),
    tr_key("в ноябре"),
    tr_key("в декабре"),
)
MONTH, THREE_MONTHS, YEAR, ALL, CUSTOM = "месяц", "3 месяца", "год", "все", "свой"
DAYS_BY_DAY, DAYS_BY_WEEK = 45, 100  # a shorter period: chart by days, by weeks, otherwise by months
KINDS = {
    catalog.SPEECH: "распознавание",
    catalog.MINUTES_KIND: "протоколы",
}  # spending kind: the function whose presets know the model
OUTSIDE_THRESHOLD = 0.005  # under half a cent off the service account is rounding, not spending outside meetings


@dataclasses.dataclass(frozen=True)
class Outcome:
    meetings_count: int = 0
    seconds: float = 0.0
    cost: float = 0.0  # everything: speech recognition and minutes
    minutes_list: float = 0.0
    by_models: tuple[tuple[str, str, float], ...] = ()  # (speech or minutes, model, how much)


@dataclasses.dataclass(frozen=True)
class Project:
    name: str
    result: Outcome


def _month(date: datetime.date, shift: int) -> datetime.date:
    """The first day of the month that is shift months away from the date."""
    number = date.year * 12 + date.month - 1 + shift
    return datetime.date(number // 12, number % 12 + 1, 1)


def month_name(number: int) -> str:
    """Month name in the interface language: "October", in Russian "октябрь"."""
    return tr(MONTHS[number - 1])


def period(
    kind: str, today: datetime.date, dates: tuple[datetime.date, datetime.date] | None = None, shift: int = 0
) -> tuple[datetime.date | None, datetime.date]:
    """Start (None: from the very beginning) and end of the period, the end is not included. A custom period: dates
    from and to picked in the calendar, both inclusive. Shift: how many such periods back (the "‹" arrow)."""
    if kind == CUSTOM and dates:
        s, by = sorted(dates)
        length = datetime.timedelta(days=(by - s).days + 1)
        return s - length * shift, s - length * shift + length
    end = today + datetime.timedelta(days=1)
    if kind == MONTH:
        return _month(today, -shift), (_month(today, 1 - shift) if shift else end)
    if kind == THREE_MONTHS:
        return _month(today, -2 - 3 * shift), (_month(today, 1 - 3 * shift) if shift else end)
    if kind == YEAR:
        year = today.year - shift
        return datetime.date(year, 1, 1), (datetime.date(year + 1, 1, 1) if shift else end)
    return None, end


def last_one(kind: str, today: datetime.date, shift: int = 0) -> tuple[datetime.date, datetime.date] | None:
    """The same period before the selected one, for comparison; "All time" and custom dates have none."""
    if kind in (MONTH, THREE_MONTHS, YEAR):
        s, by = period(kind, today, shift=shift + 1)
        return (s, by) if s else None
    return None


def row_label(
    kind: str, today: datetime.date, dates: tuple[datetime.date, datetime.date] | None = None, shift: int = 0
) -> str:
    """Period caption on the button: "October 2026", "August - October 2026", "2026", "05.10.2026 - 09.10.2026"."""
    s, by = period(kind, today, dates, shift)
    if s is None:
        return tr("All time")
    if kind == YEAR:
        return str(s.year)
    if kind == MONTH:
        return f"{month_name(s.month).capitalize()} {s.year}"
    if kind == THREE_MONTHS:
        end = _month(s, 2)
        start = month_name(s.month).capitalize() + ("" if s.year == end.year else f" {s.year}")
        return f"{start} - {month_name(end.month)} {end.year}"
    return f"{s:%d.%m.%Y} - {by - datetime.timedelta(days=1):%d.%m.%Y}"


def previous_caption(kind: str, today: datetime.date, shift: int = 0) -> str:
    """What the figures are compared with: "September", "2025", "Previous 3 months"; empty when there is nothing."""
    old = last_one(kind, today, shift)
    if old is None:
        return ""
    if kind == MONTH:
        return tr(IN_MONTH[old[0].month - 1]) + ("" if old[0].year == today.year else f" {old[0].year}")
    return tr("{year}", year=old[0].year) if kind == YEAR else tr("Previous 3 months")


def for_period(meetings: list[catalog.Meeting], s: datetime.date | None, by: datetime.date) -> list[catalog.Meeting]:
    """Recordings and transcripts in the period; ready files not yet taken in are not counted."""
    return [
        v for v in meetings if v.status != catalog.FILE and (s is None or v.date.date() >= s) and v.date.date() < by
    ]


@dataclasses.dataclass(frozen=True)
class SummaryRow:
    """A meeting in the summary table: its spending in the period."""

    folder: pathlib.Path | None  # None: the meeting is deleted, its spending remains
    title: str
    date: datetime.datetime
    seconds: float
    result: Outcome


def _spending_for(
    expenses_: list[spending.Expense], s: datetime.date | None, by: datetime.date
) -> list[spending.Expense]:
    return [t_ for t_ in expenses_ if (s is None or t_.when.date() >= s) and t_.when.date() < by]


def result(
    meetings: list[catalog.Meeting],
    s: datetime.date | None,
    by: datetime.date,
    expenses_: list[spending.Expense] | None = None,
) -> Outcome:
    """How many meetings and hours in the period and how much money went in it, by model too. Money comes from the
    spending log on the day spent; without the log, from the meeting prices."""
    meetings_ = for_period(meetings, s, by)
    custom_items = _spending_for(spending.from_meetings(meetings) if expenses_ is None else expenses_, s, by)
    models_: dict[tuple[str, str], float] = {}
    for t_ in custom_items:
        models_[(t_.kind, t_.model)] = models_.get((t_.kind, t_.model), 0) + t_.how_many
    by_models = sorted(((v, m, s) for (v, m), s in models_.items()), key=lambda r: (list(KINDS).index(r[0]), -r[2]))
    return Outcome(
        len(meetings_),
        sum(v.seconds for v in meetings_),
        sum(t_.how_many for t_ in custom_items),
        sum(t_.how_many for t_ in custom_items if t_.kind == catalog.MINUTES_KIND),
        tuple(by_models),
    )


def by_meetings(
    meetings: list[catalog.Meeting],
    s: datetime.date | None,
    by: datetime.date,
    expenses_: list[spending.Expense] | None = None,
) -> list[SummaryRow]:
    """Meetings of the period and meetings with spending in it, deleted ones too: the rows add up to the total."""
    custom_items = _spending_for(spending.from_meetings(meetings) if expenses_ is None else expenses_, s, by)
    exists = {v.folder.name: v for v in meetings if v.status != catalog.FILE}
    names = [v.folder.name for v in for_period(meetings, s, by)]
    names += sorted({t_.meeting for t_ in custom_items} - set(names))
    lines, by_name = [], {}
    for t_ in custom_items:
        by_name.setdefault(t_.meeting, []).append(t_)
    for name in names:
        meeting_spending = by_name.get(name, [])
        if (v := exists.get(name)) is not None:
            lines.append(
                SummaryRow(
                    v.folder, v.display_title, v.date, v.seconds, result([v], None, datetime.date.max, meeting_spending)
                )
            )
        else:
            title = meeting_spending[-1].title or catalog.default_title(pathlib.Path(name), view_state=True)
            lines.append(
                SummaryRow(
                    None,
                    title,
                    min(t_.when for t_ in meeting_spending),
                    0.0,
                    result([], None, datetime.date.max, meeting_spending),
                )
            )
    return lines


def by_kinds(result_: Outcome) -> list[tuple[str, float]]:
    """How much went to speech and how much to minutes: the parts of a spending chart bar."""
    return [(catalog.SPEECH, result_.cost - result_.minutes_list), (catalog.MINUTES_KIND, result_.minutes_list)]


def outside_meetings(result_: Outcome, by_account: float | None) -> float:
    """How much the service account shows spent beyond the meeting spending: checks, other programs on the same
    key, spending before the log. No account, or it is not bigger: 0."""
    if by_account is None or by_account - result_.cost < OUTSIDE_THRESHOLD:
        return 0.0
    return by_account - result_.cost


def models(result_: Outcome, total: float | None = None) -> list[tuple[str, str, float, float]]:
    """(speech or minutes, model name, how much, share of the whole: of the total spending or of the service
    account); the kind as in the catalog, untranslated."""
    total = total or result_.cost
    return [
        (kind, providers.model_title(model, KINDS[kind]) if model else "", how_many, how_many / total)
        for kind, model, how_many in result_.by_models
    ]


def arrangement(result_: Outcome, total: float | None = None) -> list[str]:
    """A line like "Speech · ElevenLabs Scribe v2 · 0,14 $ · 44%" for each model."""
    return [
        " · ".join(ch for ch in (tr(kind), name, catalog.money(how_many), f"{share:.0%}") if ch)
        for kind, name, how_many, share in models(result_, total)
    ]


def _starts(s: datetime.date, by: datetime.date) -> tuple[str, list[datetime.date]]:
    """Chart step starts: days for a short period, Mondays for a medium one, first days of months for a long one."""
    days_count = (by - s).days
    if days_count <= DAYS_BY_DAY:
        return "дням", [s + datetime.timedelta(days=n) for n in range(days_count)]
    if days_count <= DAYS_BY_WEEK:
        first_one = s - datetime.timedelta(days=s.weekday())
        return "неделям", [
            first_one + datetime.timedelta(weeks=n)
            for n in range((by - first_one).days // 7 + 1)
            if first_one + datetime.timedelta(weeks=n) < by
        ]
    return "месяцам", [
        _month(s, n) for n in range((by.year - s.year) * 12 + by.month - s.month + 1) if _month(s, n) < by
    ]


def by_steps(
    meetings: list[catalog.Meeting],
    s: datetime.date | None,
    by: datetime.date,
    expenses_: list[spending.Expense] | None = None,
) -> tuple[str, list[tuple[str, Outcome]]]:
    """Chart series for the period: the step ("дням", "неделям", "месяцам": by days, weeks, months) and the total of
    each step with its caption. Without a period start, from the month of the first meeting or spending. A step
    that began before the period counts from the period start."""
    expenses_ = spending.from_meetings(meetings) if expenses_ is None else expenses_
    if s is None:
        dates = [v.date.date() for v in meetings if v.status != catalog.FILE] + [t_.when.date() for t_ in expenses_]
        s = _month(min(dates) if dates else by, 0)
    step, starts = _starts(s, by)
    rows = []
    for number, start in enumerate(starts):
        end = starts[number + 1] if number + 1 < len(starts) else by
        row_label_ = month_name(start.month)[:3] if step == "месяцам" else _day(start)
        rows.append((row_label_, result(meetings, max(start, s), end, expenses_)))
    return step, rows


def _day(date: datetime.date) -> str:
    """Day or week caption on the axis: "1.10", in English "Oct 1"."""
    if language.current() == "ru":
        return f"{date.day}.{date.month:02d}"
    return f"{month_name(date.month)[:3]} {date.day}"


def by_projects(
    meetings: list[catalog.Meeting],
    s: datetime.date | None,
    by: datetime.date,
    expenses_: list[spending.Expense] | None = None,
) -> list[Project]:
    """Projects by hours, descending; meetings without a project as a separate row at the end."""
    expenses_ = spending.from_meetings(meetings) if expenses_ is None else expenses_
    meetings = for_period(meetings, s, by)
    names = {v.project for v in meetings} | {t_.project for t_ in _spending_for(expenses_, s, by)}
    projects = [
        Project(
            name or catalog.NO_PROJECT,
            result([v for v in meetings if v.project == name], s, by, [t_ for t_ in expenses_ if t_.project == name]),
        )
        for name in names
    ]
    return sorted(projects, key=lambda p: (p.name == catalog.NO_PROJECT, -p.result.seconds, p.name.lower()))


def hours_left(balance: float, meetings: list[catalog.Meeting]) -> float | None:
    """Meeting hours the balance covers at the average hour price of past transcripts; None: nothing to count from."""
    recognized_items = [v for v in meetings if v.cost and v.seconds]
    hours = sum(v.seconds for v in recognized_items) / 3600
    price = sum(v.cost for v in recognized_items) / hours if hours else 0
    return balance / price if price else None
