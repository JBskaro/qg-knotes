# SPDX-License-Identifier: GPL-3.0-only
"""Spending log: each paid request is a line; repeated minutes and transcription do not overwrite the earlier
spending, a deleted meeting does not take it away, the log starts with the costs already saved in the meetings."""

import datetime
import json
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from knotes import catalog, providers, minutes, transcript, summary, spending

D = datetime.datetime


def _model_response(cost: float) -> tuple[int, str]:
    return 200, json.dumps(
        {"choices": [{"message": {"content": "# Итоги\n"}, "finish_reason": "stop"}], "usage": {"cost": cost}}
    )


class TestSpendingLog(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.recordings = pathlib.Path(self._tmp.name)
        self.folder = self._meeting("2026-10-20_10-00", 0.10, "Склад")
        catalog.set_(self.folder, minutes_cost=0.03, minutes_model="claude")
        openrouter = providers.find("openrouter", "протоколы")
        self.params = minutes.Params(openrouter, "к", "claude", "Инструкция")

    def tearDown(self):
        self._tmp.cleanup()

    def _meeting(self, name: str, cost: float, project: str = "") -> pathlib.Path:
        folder = self.recordings / name
        folder.mkdir()
        transcript.save(
            folder,
            [{"начало": 1.0, "конец": 2.0, "кто": "Я", "текст": "Начнем."}],
            {"длительность": 600, "провайдер": "OpenRouter, scribe", "стоимость": cost},
            [],
        )
        if project:
            catalog.set_(folder, project=project)
        return folder

    def _total(self) -> float:
        meetings = catalog.list_(self.recordings)
        return summary.result(meetings, None, datetime.date.max, spending.list_(self.recordings, meetings)).cost

    def test_without_log_spending_from_meeting_prices(self):
        meetings = catalog.list_(self.recordings)
        self.assertEqual(
            [
                (tr.meeting, tr.kind, tr.model, round(tr.how_many, 2), tr.project, tr.when)
                for tr in spending.list_(self.recordings, meetings)
            ],
            [
                ("2026-10-20_10-00", catalog.SPEECH, "scribe", 0.10, "Склад", D(2026, 10, 20, 10, 0)),
                ("2026-10-20_10-00", catalog.MINUTES_KIND, "claude", 0.03, "Склад", D(2026, 10, 20, 10, 0)),
            ],
        )
        self.assertFalse((self.recordings / spending.FILE).exists())  # reading does not create the log

    def test_repeated_minutes_add_spending_not_overwrite(self):
        for price in (0.05, 0.04):
            with mock.patch.object(providers, "_send_json", return_value=_model_response(price)):
                minutes.compose(self.folder, self.params)
        self.assertAlmostEqual(self._total(), 0.10 + 0.03 + 0.05 + 0.04)
        self.assertAlmostEqual(catalog.meeting(self.folder).minutes_cost, 0.04)  # the meeting keeps the last one

    def test_deleted_meeting_keeps_its_spending(self):
        spending.write(self.folder, catalog.MINUTES_KIND, "claude", 0.05, D(2026, 10, 24, 9, 0))
        shutil.rmtree(self.folder)
        self.assertAlmostEqual(self._total(), 0.18)
        remaining_items = spending.list_(self.recordings, [])
        self.assertEqual({tr.project for tr in remaining_items}, {"Склад"})  # project as at the time of spending

    def test_current_meeting_project_and_broken_line(self):
        spending.write(self.folder, catalog.SPEECH, "scribe", 0.02)
        catalog.set_(self.folder, project="Бухгалтерия")  # project set after the spending
        with (self.recordings / spending.FILE).open("a", encoding="utf-8") as file:
            file.write("{оборвалась запи\n")
        meetings = catalog.list_(self.recordings)
        list_ = spending.list_(self.recordings, meetings)
        self.assertEqual(len(list_), 3)
        self.assertEqual({tr.project for tr in list_}, {"Бухгалтерия"})

    def test_free_is_not_logged(self):
        spending.write(self.folder, catalog.SPEECH, "локальная", 0.0)
        self.assertFalse((self.recordings / spending.FILE).exists())


class TestSummaryBySpending(unittest.TestCase):
    """Money by the spending day: minutes of a September meeting written in October are October spending."""

    def setUp(self):
        self.september = catalog.Meeting(
            pathlib.Path("2026-09-30_11-00"), catalog.READY, seconds=1200, cost=0.10, project="Склад"
        )
        self.spending = [
            spending.Expense(D(2026, 9, 30, 11, 0), "2026-09-30_11-00", catalog.SPEECH, "scribe", 0.10, "Склад"),
            spending.Expense(D(2026, 10, 2, 9, 0), "2026-09-30_11-00", catalog.MINUTES_KIND, "claude", 0.05, "Склад"),
            spending.Expense(
                D(2026, 10, 3, 9, 0), "2026-10-01_10-00", catalog.SPEECH, "scribe", 0.07, "", "Удаленная планерка"
            ),
        ]

    def test_total_and_projects_by_spending_day(self):
        s, by = datetime.date(2026, 10, 1), datetime.date(2026, 11, 1)
        result = summary.result([self.september], s, by, self.spending)
        self.assertEqual((result.meetings_count, round(result.cost, 2), round(result.minutes_list, 2)), (0, 0.12, 0.05))
        projects = {
            p.name: round(p.result.cost, 2) for p in summary.by_projects([self.september], s, by, self.spending)
        }
        self.assertEqual(projects, {"Склад": 0.05, catalog.NO_PROJECT: 0.07})

    def test_outside_meetings_by_service_account(self):
        """The difference with the service account only when it is above the meeting spending; model shares are of
        the whole account."""
        result = summary.Outcome(
            cost=0.46, minutes_list=0.18, by_models=((catalog.SPEECH, "", 0.28), (catalog.MINUTES_KIND, "", 0.18))
        )
        self.assertAlmostEqual(summary.outside_meetings(result, 1.48), 1.02)
        for account in (None, 0.46, 0.4601, 0.30):
            with self.subTest(account=account):
                self.assertEqual(summary.outside_meetings(result, account), 0.0)
        self.assertEqual([round(share, 2) for *_, share in summary.models(result, 1.48)], [0.19, 0.12])
        self.assertEqual([round(share, 2) for *_, share in summary.models(result)], [0.61, 0.39])

    def test_rows_by_meetings_with_deleted(self):
        lines = summary.by_meetings(
            [self.september], datetime.date(2026, 10, 1), datetime.date(2026, 11, 1), self.spending
        )
        self.assertEqual(
            [(s.title, s.folder is not None, round(s.result.cost, 2)) for s in lines],
            [(self.september.display_title, True, 0.05), ("Удаленная планерка", False, 0.07)],
        )
        self.assertAlmostEqual(sum(s.result.cost for s in lines), 0.12)  # the rows add up to the total


if __name__ == "__main__":
    unittest.main()
