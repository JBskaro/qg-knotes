# SPDX-License-Identifier: GPL-3.0-only
"""What the summary window shows, counted without Qt: the figures with their hints, the chart series, the breakdowns
by project and by model and the table rows for the selected period and project. What the key balance changes is
counted apart, when the balance comes. Money and numbers are written as the interface language writes them."""

import dataclasses
import datetime

from knotes import catalog, failures, language, providers, spending, summary
from knotes.language import tr, tr_key

CHART_TITLES = {
    "дням": (tr_key("Расходы по дням"), tr_key("Часы встреч по дням")),  # chart step -> headings
    "неделям": (tr_key("Расходы по неделям"), tr_key("Часы встреч по неделям")),
    "месяцам": (tr_key("Расходы по месяцам"), tr_key("Часы встреч по месяцам")),
}


def number(value: float) -> str:
    """No trailing zeros, the decimal mark of the interface language: 0,25 and 2 in Russian, 0.25 in English."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") if language.current() == "ru" else text


def dollars(text: str) -> str:
    """A sum as the interface language writes it: 0,8 $ in Russian, $0.8 in English."""
    return catalog.currency_amount(text, "USD")


def money(amount: float, currency: str = "USD") -> str:
    return catalog.money(amount, currency) if amount else catalog.currency_amount("0", currency)


def clock(seconds: float) -> str:
    return catalog.duration(seconds) if seconds >= 30 else tr("0 min")


def or_dash(amount: float, currency: str = "USD") -> str:
    """For a narrow column: no money is a dash, less than a cent is "< 0,01 $"."""
    if not amount:
        return "-"
    return "< " + catalog.currency_amount(number(0.01), currency) if amount < 0.01 else catalog.money(amount, currency)


def _before(when: str, how_many: object) -> str:
    """The previous period's figure in the tile hint: in Russian "в сентябре 0,40 $", in English "September: 0,40 $"."""
    return tr("{when}: {how_many}", when=when, how_many=how_many)


def title(project: str | None, name: str) -> str:
    """The window title for the selected project; name is the program name."""
    if project is None:
        return tr("Summary - {name}", name=name)
    if not project:
        return tr("Summary - no project - {name}", name=name)
    return tr("Summary - project {project} - {name}", project=project, name=name)


@dataclasses.dataclass(frozen=True)
class Selection:
    """The filters: the kind of period, how many such periods back, the custom dates (both inclusive; their shift is
    already in them) and the project: None is all projects, "" meetings without a project."""

    kind: str = summary.MONTH
    shift: int = 0
    dates: tuple[datetime.date, datetime.date] | None = None
    project: str | None = None
    currency: str = "USD"

    def period(self, today: datetime.date) -> tuple[datetime.date | None, datetime.date]:
        return summary.period(self.kind, today, self.dates, 0 if self.kind == summary.CUSTOM else self.shift)

    def previous(self, today: datetime.date) -> tuple[str, tuple[datetime.date, datetime.date]] | None:
        """What to compare with: the caption ("in September") and the same kind of period before the selected one."""
        old = summary.last_one(self.kind, today, self.shift)
        return (summary.previous_caption(self.kind, today, self.shift), old) if old else None


@dataclasses.dataclass(frozen=True)
class Figure:
    """A summary tile: the value and the hint under it; share fills its strip."""

    value: str
    hint: str = ""
    share: float | None = None


Bars = list[tuple[str, tuple[float, ...]]]  # chart bars: the caption and the parts stacked from the bottom


@dataclasses.dataclass(frozen=True)
class Report:
    """The summary for one selection, calculated from the local spending journal."""

    selection: Selection
    result: summary.Outcome
    before: str  # the previous period's spending: the first line of the "Spent" hint
    meetings: Figure
    hours: Figure
    headings: tuple[str, str]  # of the cost chart and of the hours chart
    costs: Bars  # each bar of two parts: speech and minutes
    cost_tips: list[str]
    hours_bars: Bars
    current: int | None  # the step that is now, drawn darker
    projects: list[summary.Project]
    rows: list[summary.SummaryRow]


def report(
    meetings: list[catalog.Meeting],
    expenses: list[spending.Expense],
    selection: Selection,
    today: datetime.date,
) -> Report:
    """The figures, charts, projects and table rows for the selection, from the meetings and spending read once; a
    deleted meeting's project is the one at the time of spending."""
    expenses = [expense for expense in expenses if expense.currency == selection.currency]
    if selection.project is not None:
        meetings = [v for v in meetings if v.project == selection.project]
        expenses = [t_ for t_ in expenses if t_.project == selection.project]
    s, by = selection.period(today)
    result = summary.result(meetings, s, by, expenses)
    before_cost = before_count = ""
    if previous := selection.previous(today):
        when, (old_s, old_by) = previous
        old = summary.result(meetings, old_s, old_by, expenses)
        before_cost, before_count = (
            _before(when, money(old.cost, selection.currency)),
            _before(when, old.meetings_count),
        )
    average = (
        tr("{time} on average", time=clock(result.seconds / result.meetings_count)) if result.meetings_count else ""
    )
    step, steps = summary.by_steps(meetings, s, by, expenses)
    return Report(
        selection=selection,
        result=result,
        before=before_cost,
        meetings=Figure(str(result.meetings_count), before_count),
        hours=Figure(clock(result.seconds), average),
        headings=(tr(CHART_TITLES[step][0]), tr(CHART_TITLES[step][1])),
        costs=[(p, tuple(how_many for _, how_many in summary.by_kinds(i))) for p, i in steps],
        cost_tips=[
            "\n".join(
                [
                    tr("{step} · total {amount}", step=p, amount=money(i.cost, selection.currency)),
                    *summary.arrangement(i, selection.currency),
                ]
            )
            for p, i in steps
        ],
        hours_bars=[(p, (i.seconds / 3600,)) for p, i in steps],
        current=len(steps) - 1 if by == today + datetime.timedelta(days=1) else None,  # the last step is now
        projects=summary.by_projects(meetings, s, by, expenses),
        rows=summary.by_meetings(meetings, s, by, expenses),
    )


@dataclasses.dataclass(frozen=True)
class Line:
    """A breakdown row: the name, its strip and the figure on the right, the tooltip for the whole row. The strip has
    the color of the bar part (kind: 0 speech, 1 minutes) or of the project, if it has its own; a quiet row (outside
    meetings, without a project) is gray."""

    name: str
    share: float
    figure: str
    tooltip: str
    kind: int = 0
    project: str = ""
    quiet: bool = False


def project_lines(projects: list[summary.Project], currency: str = "USD") -> list[Line]:
    """Projects by hours, with the strip relative to the longest one."""
    largest = max((p.result.seconds for p in projects), default=0) or 1
    lines = []
    for p in projects:
        without = p.name == catalog.NO_PROJECT
        name = tr(p.name) if without else p.name
        split = catalog.breakdown(p.result.cost, p.result.minutes_list, currency)
        tooltip = tr(
            "{project} · meetings: {n} · {clock} · {amount}",
            project=name,
            n=p.result.meetings_count,
            clock=clock(p.result.seconds),
            amount=money(p.result.cost, currency),
        )
        lines.append(
            Line(
                name=name,
                share=p.result.seconds / largest,
                figure=f"{clock(p.result.seconds)} · {money(p.result.cost, currency)}",
                tooltip=tooltip + (f" ({split})" if split else ""),
                project="" if without else p.name,
                quiet=without,
            )
        )
    return lines


def model_lines(result: summary.Outcome, currency: str = "USD") -> list[Line]:
    """Models for the period and their shares of the recorded spending."""
    kinds = list(summary.KINDS)
    return [
        Line(
            name=" · ".join(ch for ch in (tr(kind), name) if ch),
            share=share,
            figure=f"{money(how_many, currency)} · {share:.0%}",
            tooltip=line,
            kind=kinds.index(kind),
        )
        for (kind, name, how_many, share), line in zip(
            summary.models(result), summary.arrangement(result, currency), strict=True
        )
    ]


def spent_figure(report_: Report, error: str = "") -> Figure:
    hint = "\n".join(filter(None, (tr("From the local spending journal"), report_.before, error)))
    return Figure(money(report_.result.cost, report_.selection.currency), hint)


def balance_figure(balance: object, provider: str, meetings: list[catalog.Meeting]) -> Figure:
    """The key balance tile: the balance with the hours of meetings it is enough for, or why it is not known.
    balance is what the service answered: a Balance, None (it does not report one) or the error."""
    if isinstance(balance, Exception):
        return Figure("?", tr("Couldn't check: {error}", error=failures.text(balance)), 0)
    if not isinstance(balance, providers.Balance):  # None: the service does not report one
        return Figure("-", tr("{provider} doesn't report the balance", provider=provider), 0)
    total = dollars(number(balance.total))
    source_of = (
        tr("of {total}", total=total) if balance.source_of == "ключ" else tr("on the account, of {total}", total=total)
    )
    hours = summary.hours_left(balance.remaining, meetings)
    hint = (
        tr("{source_of}, enough for {hours} h of meetings", source_of=source_of, hours=round(hours))
        if hours
        else source_of
    )
    if balance.by_key is not None:
        hint += "\n" + tr("Spent on this key: {amount}", amount=money(balance.by_key))
    return Figure(catalog.money(balance.remaining), hint, balance.remaining / balance.total if balance.total else 0)
