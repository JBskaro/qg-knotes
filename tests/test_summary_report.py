# SPDX-License-Identifier: GPL-3.0-only
"""What the summary shows, counted without a window: figures and their hints against the previous period, chart
series, breakdowns with spending outside meetings, "Spent" by the service account, the key balance tile."""

import datetime
import pathlib
import unittest

from knotes import catalog, language, providers, spending, summary, summary_report
from knotes.summary_report import Selection

TODAY = datetime.date(2026, 10, 22)
SCRIBE, SONNET = "elevenlabs/scribe-v2", "anthropic/claude-sonnet-5.5"


def meeting(name: str, seconds: float, cost: float, project: str = "", minutes_cost: float = 0.0) -> catalog.Meeting:
    return catalog.Meeting(
        pathlib.Path(name),
        catalog.READY,
        seconds=seconds,
        cost=cost,
        project=project,
        model=SCRIBE,
        minutes_cost=minutes_cost,
        minutes_model=SONNET if minutes_cost else "",
    )


MEETINGS = [
    meeting("2026-10-22_10-00", 1800, 0.40, "Склад", minutes_cost=0.30),
    meeting("2026-10-20_12-00", 600, 0.10, "Отчеты"),
    meeting("2026-09-03_09-00", 1200, 0.25),
]
EXPENSES = spending.from_meetings(MEETINGS)


def _report(selection: Selection | None = None, today: datetime.date = TODAY) -> summary_report.Report:
    return summary_report.report(MEETINGS, EXPENSES, selection or Selection(), today)


class TestFormat(unittest.TestCase):
    def test_numbers_and_money_in_interface_language(self):
        self.assertEqual(
            [summary_report.number(0.25), summary_report.number(2.0), summary_report.dollars("0,8")],
            ["0,25", "2", "0,8 $"],
        )
        self.assertEqual((summary_report.money(0), summary_report.money(1.5)), ("0 $", "1,50 $"))
        self.assertEqual((summary_report.clock(20), summary_report.clock(1800)), ("0 мин", "30 мин"))
        self.assertEqual([summary_report.or_dash(z) for z in (0, 0.004, 0.4)], ["-", "< 0,01 $", "0,40 $"])
        try:
            language.set_("en")
            self.assertEqual((summary_report.number(0.25), summary_report.dollars("0.8")), ("0.25", "$0.8"))
        finally:
            language.set_("ru")

    def test_title_by_project(self):
        self.assertEqual(summary_report.title(None, "Узелок"), "Сводка - Узелок")
        self.assertEqual(summary_report.title("", "Узелок"), "Сводка - без проекта - Узелок")
        self.assertEqual(summary_report.title("Склад", "Узелок"), "Сводка - проект Склад - Узелок")


class TestReport(unittest.TestCase):
    def test_selection_period_and_previous(self):
        d = datetime.date
        self.assertEqual(Selection(summary.MONTH, 1).period(TODAY), (d(2026, 9, 1), d(2026, 10, 1)))
        custom = Selection(summary.CUSTOM, 3, (d(2026, 10, 5), d(2026, 10, 9)))
        self.assertEqual(custom.period(TODAY), (d(2026, 10, 5), d(2026, 10, 10)))  # the shift is in the dates
        self.assertIsNone(custom.previous(TODAY))
        self.assertEqual(Selection().previous(TODAY), ("в сентябре", (d(2026, 9, 1), d(2026, 10, 1))))

    def test_figures_against_previous_period(self):
        report = _report()
        self.assertEqual((report.result.meetings_count, round(report.result.cost, 2)), (2, 0.5))
        self.assertEqual(report.meetings, summary_report.Figure("2", "в сентябре 1"))
        self.assertEqual(report.hours, summary_report.Figure("40 мин", "в среднем 20 мин"))
        self.assertEqual(report.before, "в сентябре 0,25 $")
        empty = _report(Selection(summary.ALL), datetime.date(2026, 1, 1))
        self.assertEqual((empty.meetings, empty.hours.hint, empty.before), (summary_report.Figure("0", ""), "", ""))

    def test_charts_by_step_with_the_current_one(self):
        report = _report()
        self.assertEqual(report.headings, ("Расходы по дням", "Часы встреч по дням"))
        self.assertEqual((len(report.costs), report.current), (22, 21))  # the last day is today
        self.assertEqual([round(z, 2) for z in report.costs[-1][1]], [0.1, 0.3])
        self.assertEqual(report.hours_bars[19], ("20.10", (600 / 3600,)))
        self.assertEqual(report.cost_tips[0], "1.10 · всего 0 $")
        self.assertIn("Протоколы · Claude Sonnet 5.5 · 0,30 $ · 75%", report.cost_tips[-1])
        past = _report(Selection(summary.MONTH, 1))
        self.assertIsNone(past.current)
        self.assertEqual(_report(Selection(summary.YEAR)).headings[0], "Расходы по месяцам")
        self.assertEqual(_report(Selection(summary.ALL)).headings[1], "Часы встреч по неделям")

    def test_project_selection_counts_only_its_meetings(self):
        report = _report(Selection(project="Склад"))
        self.assertEqual((report.meetings.value, [r.title for r in report.rows]), ("1", ["Встреча 22.10 10:00"]))
        self.assertEqual(report.meetings.hint, "в сентябре 0")
        without = _report(Selection(summary.ALL, project=""))
        self.assertEqual([p.name for p in without.projects], [catalog.NO_PROJECT])


class TestLines(unittest.TestCase):
    def test_projects_with_split_and_quiet_rows_last(self):
        lines = summary_report.project_lines(_report(Selection(summary.ALL)).projects, 0.98)
        self.assertEqual([line.name for line in lines], ["Склад", "Отчеты", "Без проекта", "Вне встреч"])
        first = lines[0]
        self.assertEqual(
            (first.share, first.figure, first.project, first.quiet), (1.0, "30 мин · 0,40 $", "Склад", False)
        )
        self.assertEqual(first.tooltip, "Склад: встреч 1, 30 мин, 0,40 $ (речь 0,10 $, протоколы 0,30 $)")
        self.assertAlmostEqual(lines[1].share, 600 / 1800)
        self.assertEqual((lines[2].project, lines[2].quiet), ("", True))
        self.assertEqual((lines[3].figure, lines[3].share, lines[3].quiet), ("0,98 $", 0, True))
        self.assertEqual(summary_report.project_lines([]), [])

    def test_models_share_of_the_whole_account(self):
        result = _report().result
        lines = summary_report.model_lines(result, 0.5)
        self.assertEqual(
            [(line.name, line.figure, line.kind) for line in lines],
            [
                ("Речь · ElevenLabs Scribe v2", "0,20 $ · 20%", 0),
                ("Протоколы · Claude Sonnet 5.5", "0,30 $ · 30%", 1),
                ("Вне встреч", "0,50 $ · 50%", 0),
            ],
        )
        self.assertTrue(lines[-1].quiet)
        self.assertEqual(lines[0].tooltip, "Речь · ElevenLabs Scribe v2 · 0,20 $ · 20%")
        self.assertEqual(len(summary_report.model_lines(result)), 2)


class TestSpending(unittest.TestCase):
    BALANCE = providers.Balance(8.52, 10.0, "ключ", for_month=1.48, by_key=2.0)

    def test_account_only_for_this_month_or_all_time_of_all_projects(self):
        account = summary_report.by_account
        self.assertEqual(account(Selection(), self.BALANCE), (True, 1.48))
        self.assertEqual(account(Selection(summary.ALL), self.BALANCE), (False, 2.0))
        for selection in (Selection(shift=1), Selection(project="Склад"), Selection(summary.YEAR)):
            self.assertIsNone(account(selection, self.BALANCE), selection)
        self.assertIsNone(account(Selection(), None))
        self.assertIsNone(account(Selection(), providers.Balance(1, 2, "ключ")))  # the service has no account

    def test_spent_by_account_with_outside_meetings(self):
        spent = summary_report.spending(_report(), self.BALANCE, "OpenRouter")
        self.assertAlmostEqual(spent.outside, 0.98)
        self.assertEqual(spent.spent.value, "1,48 $")
        self.assertEqual(
            spent.spent.hint.split("\n"),
            ["в сентябре 0,25 $", "как в OpenRouter за месяц", "по встречам 0,50 $, вне встреч 0,98 $"],
        )
        self.assertEqual((spent.projects[-1].name, spent.models[-1].name), ("Вне встреч", "Вне встреч"))
        by_key = summary_report.spending(_report(Selection(summary.ALL)), self.BALANCE, "OpenRouter")
        self.assertIn("как в OpenRouter по ключу", by_key.spent.hint)

    def test_account_below_meetings_alone_and_log_error(self):
        less = providers.Balance(9.6, 10.0, "ключ", for_month=0.40, by_key=0.40)
        spent = summary_report.spending(_report(), less, "OpenRouter", "Журнал трат не прочитан")
        self.assertEqual((spent.outside, spent.spent.value), (0.0, "0,50 $"))
        self.assertEqual(
            spent.spent.hint.split("\n"),
            ["в сентябре 0,25 $", "OpenRouter за месяц 0,40 $", "Журнал трат не прочитан"],
        )
        all_time = summary_report.spending(_report(Selection(summary.ALL)), less, "OpenRouter")
        self.assertEqual(all_time.spent.hint, "OpenRouter по ключу 0,40 $")
        alone = summary_report.spending(_report(), None, "OpenRouter")
        self.assertEqual((alone.spent.hint, len(alone.models)), ("в сентябре 0,25 $", 2))

    def test_balance_tile(self):
        figure = summary_report.balance_figure(self.BALANCE, "OpenRouter", MEETINGS)
        self.assertEqual((figure.value, figure.share), ("8,52 $", 0.852))
        self.assertRegex(figure.hint, r"^из 10 \$, хватит на \d+ ч встреч$")
        account = summary_report.balance_figure(providers.Balance(3, 0, "счет"), "OpenRouter", [])
        self.assertEqual((account.hint, account.share), ("на счете, из 0 $", 0))
        failed = summary_report.balance_figure(providers.ProviderError("Сервер не ответил"), "OpenRouter", [])
        self.assertEqual((failed.value, failed.hint), ("?", "Не удалось узнать: Сервер не ответил"))
        silent = summary_report.balance_figure(None, "OpenRouter", [])
        self.assertEqual((silent.value, silent.hint), ("-", "OpenRouter не сообщает остаток"))


if __name__ == "__main__":
    unittest.main()
