# SPDX-License-Identifier: GPL-3.0-only
"""Summary: periods and their captions, totals, chart series, projects, how long the balance lasts."""

import datetime
import pathlib
import unittest

from knotes import catalog, summary

TODAY = datetime.date(2026, 10, 22)  # Thursday, the week starts on 19.10


def meeting(
    date: str, seconds: float, cost: float | None = None, project: str = "", status: str = catalog.READY
) -> catalog.Meeting:
    return catalog.Meeting(pathlib.Path(date), status, seconds=seconds, cost=cost, project=project)


MEETINGS = [
    meeting("2026-10-22_10-00", 1800, 0.12, "ЗУП"),
    meeting("2026-10-20_09-00", 3600, 0.20, "ЗУП"),
    meeting("2026-10-14_15-00", 1200, None, "", catalog.RECORDED),
    meeting("2026-09-30_11-00", 2400, 0.15),
    meeting("2026-08-05_11-00", 600, 0.05, "Бухгалтерия"),
    meeting("2026-05-05_11-00", 600, 0.04),
    catalog.Meeting(pathlib.Path("Созвон.mp4"), catalog.FILE),
]


class TestSummary(unittest.TestCase):
    def test_periods(self):
        self.assertEqual(
            summary.period(summary.MONTH, TODAY), (datetime.date(2026, 10, 1), datetime.date(2026, 10, 23))
        )
        self.assertEqual(summary.period(summary.THREE_MONTHS, TODAY)[0], datetime.date(2026, 8, 1))
        self.assertEqual(summary.period(summary.ALL, TODAY)[0], None)
        self.assertEqual(
            summary.last_one(summary.MONTH, TODAY), (datetime.date(2026, 9, 1), datetime.date(2026, 10, 1))
        )
        self.assertEqual(
            summary.last_one(summary.THREE_MONTHS, TODAY), (datetime.date(2026, 5, 1), datetime.date(2026, 8, 1))
        )
        self.assertIsNone(summary.last_one(summary.ALL, TODAY))
        self.assertEqual(summary.period(summary.MONTH, datetime.date(2026, 1, 15))[0], datetime.date(2026, 1, 1))
        self.assertEqual(summary.last_one(summary.MONTH, datetime.date(2026, 1, 15))[0], datetime.date(2025, 12, 1))

    def test_custom_period_by_calendar(self):
        """From and to dates inclusive, in any order; nothing to compare with."""
        s, by = datetime.date(2026, 10, 5), datetime.date(2026, 10, 9)
        self.assertEqual(summary.period(summary.CUSTOM, TODAY, (s, by)), (s, datetime.date(2026, 10, 10)))
        self.assertEqual(summary.period(summary.CUSTOM, TODAY, (by, s)), (s, datetime.date(2026, 10, 10)))
        self.assertIsNone(summary.last_one(summary.CUSTOM, TODAY))

    def test_arrows_to_previous_periods_and_year(self):
        d = datetime.date
        self.assertEqual(summary.period(summary.MONTH, TODAY, shift=1), (d(2026, 9, 1), d(2026, 10, 1)))
        self.assertEqual(summary.period(summary.THREE_MONTHS, TODAY, shift=1), (d(2026, 5, 1), d(2026, 8, 1)))
        self.assertEqual(summary.period(summary.YEAR, TODAY), (d(2026, 1, 1), d(2026, 10, 23)))
        self.assertEqual(summary.period(summary.YEAR, TODAY, shift=1), (d(2025, 1, 1), d(2026, 1, 1)))
        custom_one = (d(2026, 10, 5), d(2026, 10, 9))
        self.assertEqual(summary.period(summary.CUSTOM, TODAY, custom_one, 1), (d(2026, 9, 30), d(2026, 10, 5)))
        self.assertEqual(summary.last_one(summary.MONTH, TODAY, 1), (d(2026, 8, 1), d(2026, 9, 1)))
        self.assertEqual(summary.last_one(summary.YEAR, TODAY), (d(2025, 1, 1), d(2026, 1, 1)))

    def test_period_captions(self):
        d = datetime.date
        self.assertEqual(summary.row_label(summary.MONTH, TODAY), "Октябрь 2026")
        self.assertEqual(summary.row_label(summary.THREE_MONTHS, TODAY), "Август - октябрь 2026")
        self.assertEqual(summary.row_label(summary.THREE_MONTHS, d(2026, 1, 10)), "Ноябрь 2025 - январь 2026")
        self.assertEqual(summary.row_label(summary.YEAR, TODAY, shift=1), "2025")
        self.assertEqual(summary.row_label(summary.ALL, TODAY), "Все время")
        self.assertEqual(
            summary.row_label(summary.CUSTOM, TODAY, (d(2026, 10, 5), d(2026, 10, 9))), "05.10.2026 - 09.10.2026"
        )
        self.assertEqual(summary.previous_caption(summary.MONTH, TODAY), "в сентябре")
        self.assertEqual(summary.previous_caption(summary.YEAR, TODAY), "в 2025")
        self.assertEqual(summary.previous_caption(summary.ALL, TODAY), "")

    def test_chart_step_by_period_length(self):
        step, days = summary.by_steps(MEETINGS, *summary.period(summary.MONTH, TODAY))
        self.assertEqual((step, len(days), days[0][0], days[-1][0]), ("дням", 22, "1.10", "22.10"))
        self.assertEqual(days[19][1].meetings_count, 1)
        step, weeks = summary.by_steps(MEETINGS, *summary.period(summary.THREE_MONTHS, TODAY))
        self.assertEqual((step, weeks[0][0], weeks[-1][0]), ("неделям", "27.07", "19.10"))
        self.assertAlmostEqual(sum(i.seconds for _, i in weeks), 9600)
        step, months = summary.by_steps(MEETINGS, *summary.period(summary.ALL, TODAY))
        self.assertEqual((step, [m for m, _ in months]), ("месяцам", ["май", "июн", "июл", "авг", "сен", "окт"]))
        self.assertAlmostEqual(months[-1][1].cost, 0.32)

    def test_total_for_period(self):
        s, by = summary.period(summary.MONTH, TODAY)
        result = summary.result(MEETINGS, s, by)
        self.assertEqual((result.meetings_count, result.seconds), (3, 6600))
        self.assertAlmostEqual(result.cost, 0.32)
        self.assertEqual(summary.result(MEETINGS, None, by).meetings_count, 6)

    def test_by_projects(self):
        projects = summary.by_projects(MEETINGS, None, summary.period(summary.ALL, TODAY)[1])
        self.assertEqual([p.name for p in projects], ["ЗУП", "Бухгалтерия", "Без проекта"])
        self.assertEqual((projects[0].result.meetings_count, projects[0].result.seconds), (2, 5400))
        self.assertEqual(projects[-1].result.meetings_count, 3)

    def test_speech_and_minutes_costs_by_models(self):
        """For a day and for a period: how much went to speech and to minutes, by which model and what share."""
        meetings = [
            catalog.Meeting(
                pathlib.Path("2026-10-22_10-00"),
                catalog.READY,
                seconds=1800,
                cost=0.30,
                minutes_cost=0.18,
                model="elevenlabs/scribe-v2",
                minutes_model="anthropic/claude-sonnet-5.5",
            ),
            catalog.Meeting(
                pathlib.Path("2026-10-21_10-00"), catalog.READY, seconds=600, cost=0.02, model="elevenlabs/scribe-v2"
            ),
        ]
        result = summary.result(meetings, None, datetime.date(2026, 10, 23))
        self.assertEqual(
            [(f, m) for f, m, _ in result.by_models],
            [(catalog.SPEECH, "elevenlabs/scribe-v2"), (catalog.MINUTES_KIND, "anthropic/claude-sonnet-5.5")],
        )
        self.assertEqual(
            [(v, round(c_, 2)) for v, c_ in summary.by_kinds(result)], [("Речь", 0.14), ("Протоколы", 0.18)]
        )
        self.assertEqual(
            summary.arrangement(result),
            ["Речь · ElevenLabs Scribe v2 · 0,14 $ · 44%", "Протоколы · Claude Sonnet 5.5 · 0,18 $ · 56%"],
        )
        self.assertEqual(summary.by_kinds(summary.Outcome()), [("Речь", 0), ("Протоколы", 0)])
        self.assertEqual(summary.arrangement(summary.Outcome()), [])

    def test_how_long_it_lasts(self):
        self.assertAlmostEqual(summary.hours_left(1.0, MEETINGS), 1.0 / (0.56 / (9000 / 3600)))
        self.assertIsNone(summary.hours_left(1.0, [meeting("2026-10-22_10-00", 600)]))


if __name__ == "__main__":
    unittest.main()
