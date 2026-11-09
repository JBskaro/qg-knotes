# SPDX-License-Identifier: GPL-3.0-only
"""Summary: the arrow pages the period, choosing a project changes all figures and charts under the filters; it is
counted once on opening, money and numbers follow the interface language, names in tooltips are text."""

import datetime
import pathlib
import tempfile
import unittest
from unittest import mock

from PySide6.QtCore import QDate, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from knotes import catalog, language, providers, spending, transcript, summary, summary_report
from knotes.ui import summary_filters
from knotes.ui.summary_window import SummaryWindow
from tests.shown_windows import shown_windows

app = QApplication.instance() or QApplication([])


class TestSummaryWindow(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        recordings = pathlib.Path(self._temporary.name)
        self.recordings = recordings
        today = datetime.date.today()
        for name, project in ((f"{today:%Y-%m-%d}_10-00", "Склад"), (f"{today:%Y-%m-%d}_12-00", "Отчеты")):
            folder = recordings / name
            folder.mkdir()
            transcript.save(
                folder, [], {"длительность": 600, "провайдер": "OpenRouter, elevenlabs/scribe-v2", "стоимость": 0.1}, []
            )
            catalog.set_(folder, project=project)
        catalog.set_(folder, minutes_cost=0.3, minutes_model="anthropic/claude-sonnet-5.5")
        with shown_windows() as self.flashed:
            self.window = SummaryWindow(recordings, lambda: None, "OpenRouter", (["Склад", "Отчеты", "Пустой"], {}))
        self.window.display()

    def tearDown(self):
        self.window.close()
        self._temporary.cleanup()

    def test_opening_flashes_no_window(self):
        """The strip of a tile is placed in the window before it is shown."""
        self.assertEqual(self.flashed, [])

    def test_provider_without_balance_says_so(self):
        """A provider that reports no balance gets "-", not an endless "Asking"."""
        self.window._show_balance(None)
        self.assertEqual(self.window.tiles[3].value.text(), "-")

    def test_local_recognition_has_no_key_balance(self):
        """Speech recognized on this computer has no key: the tile says it is free instead of asking anyone."""
        window = SummaryWindow(self.recordings, None, "На этом компьютере", (["Склад"], {}))
        self.addCleanup(window.close)
        window.display()
        window.refresh_balance()
        self.assertEqual(window.tiles[3].value.text(), "-")
        self.assertIn("бесплатно", window.tiles[3].hint.text())

    def test_arrow_and_project(self):
        filters, today = self.window.filters, datetime.date.today()
        self.assertEqual(filters._period.text(), summary.row_label(summary.MONTH, today))
        self.assertEqual(self.window.tiles[1].value.text(), "2")
        filters._step(1)
        self.assertEqual(filters._period.text(), summary.row_label(summary.MONTH, today, shift=1))
        self.assertEqual(self.window.tiles[1].value.text(), "0")
        self.assertTrue(filters._forward.isEnabled())
        filters._step(-1)
        filters._pick_project("Склад")
        self.assertEqual((filters._project.text(), self.window.tiles[1].value.text()), ("Склад", "1"))
        self.assertEqual(self.window._headings[0].text(), "Расходы по дням")

    def test_speech_and_minutes_costs_shown_by_models(self):
        """A day bar of two parts, on hover the models and shares; under the charts, rows by model."""
        tile = self.window.tiles[0]
        self.assertEqual(tile.value.text(), "0,50 $")
        self.assertNotIn("речь", tile.hint.text())  # the breakdown is in "By model", not repeated in the tile
        self.assertEqual(self.window.expenses.data[-1][1], (0.2, 0.3))
        self.assertIn("Протоколы · Claude Sonnet 5.5 · 0,30 $ · 60%", self.window.expenses.tips[-1])
        lines = [
            self.window._models.itemAtPosition(r, 0).widget().text()
            for r in range(self.window._models.rowCount())
            if self.window._models.itemAtPosition(r, 0)
        ]
        self.assertEqual(lines, ["Речь · ElevenLabs Scribe v2", "Протоколы · Claude Sonnet 5.5"])
        self.assertIn("Речь", self.window.legend.text())
        self.window.display()  # after a recount the rows start from the first again
        self.assertEqual(self.window._models.itemAtPosition(0, 0).widget().text(), "Речь · ElevenLabs Scribe v2")

    @staticmethod
    def _lines(grid_layout) -> list[tuple[str, str]]:
        """Breakdown rows: (name, total on the right)."""
        return [
            (grid_layout.itemAtPosition(r, 0).widget().text(), grid_layout.itemAtPosition(r, 2).widget().text())
            for r in range(grid_layout.rowCount())
            if grid_layout.itemAtPosition(r, 2)
        ]

    def test_spent_like_provider_and_difference_as_outside_row(self):
        """The "Spent" tile shows what went from the key by the OpenRouter account; what is not for this program's
        meetings (checks, other programs on the same key) is an "Outside meetings" row in the breakdowns and
        the table: everything adds up to the tile."""
        tile, filters = self.window.tiles[0], self.window.filters
        self.window._show_balance(providers.Balance(8.52, 10.0, "ключ", 1.48, 2.0))
        self.assertEqual(tile.value.text(), "1,48 $")
        self.assertIn("как в OpenRouter за месяц", tile.hint.text())
        self.assertIn("по встречам 0,50 $, вне встреч 0,98 $", tile.hint.text())
        self.assertEqual(self._lines(self.window._models)[-1], ("Вне встреч", "0,98 $ · 66%"))
        self.assertEqual(self._lines(self.window._models)[0], ("Речь · ElevenLabs Scribe v2", "0,20 $ · 14%"))
        self.assertEqual(self._lines(self.window._projects)[-1], ("Вне встреч", "0,98 $"))
        table = self.window.by_meetings
        outside = next(r for r in range(table.rowCount()) if table.item(r, 0).text() == "Вне встреч")
        self.assertEqual(table.item(outside, 5).text(), "0,98 $")
        filters._pick_project("Склад")  # the service does not count by project
        self.assertEqual(tile.value.text(), "0,10 $")
        self.assertNotIn("Вне встреч", [name for name, _ in self._lines(self.window._models)])
        filters._pick_project(None)
        filters._pick_period(summary.ALL)
        self.assertEqual(tile.value.text(), "2,00 $")
        self.assertIn("как в OpenRouter по ключу", tile.hint.text())
        filters._pick_period(summary.MONTH, 1)  # the service does not report the previous month
        self.assertNotIn("OpenRouter", tile.hint.text())
        filters._pick_period(summary.MONTH)
        self.window._show_balance(providers.Balance(9.6, 10.0, "ключ", 0.40, 0.40))  # service shows less: a new key
        self.assertEqual(tile.value.text(), "0,50 $")
        self.assertIn("OpenRouter за месяц 0,40 $", tile.hint.text())
        self.assertNotIn("Вне встреч", [name for name, _ in self._lines(self.window._models)])

    def test_per_meeting_prices_and_models(self):
        """Prices and models of each meeting only here; a double click on a row opens the meeting."""
        table = self.window.by_meetings
        self.assertEqual(table.rowCount(), 2)
        rows = {
            table.item(r, 0).text(): [table.item(r, k).text() for k in range(table.columnCount())]
            for r in range(table.rowCount())
        }
        with_minutes_text = next(r for r in rows.values() if r[4] != "-")
        self.assertEqual(with_minutes_text[3:6], ["0,10 $", "0,30 $", "0,40 $"])
        self.assertIn("Claude Sonnet 5.5", with_minutes_text[6])
        opened_all: list[pathlib.Path] = []
        self.window.open_meeting.connect(opened_all.append)
        table.cellDoubleClicked.emit(0, 0)
        self.assertEqual(len(opened_all), 1)

    def test_counted_once_on_opening_and_balance_keeps_the_rest(self):
        """The window reads the folder and the spending log once; the balance does not count the charts again."""
        with (
            mock.patch.object(catalog, "list_", wraps=catalog.list_) as meetings,
            mock.patch.object(spending, "list_", wraps=spending.list_) as log_,
            mock.patch.object(summary, "by_steps", wraps=summary.by_steps) as charts,
        ):
            window = SummaryWindow(self.recordings, lambda: None, "OpenRouter", (["Склад"], {}))
            self.addCleanup(window.close)
            window.display()  # as the app does right after opening
            window._show_balance(providers.Balance(8.52, 10.0, "ключ", 1.48, 2.0))
            window._balance_tile()
        self.assertEqual((meetings.call_count, log_.call_count, charts.call_count), (1, 1, 1))
        self.assertEqual(window.tiles[0].value.text(), "1,48 $")

    def test_typed_dates_counted_once_they_stop(self):
        filters = self.window.filters
        filters._pick_period(summary.CUSTOM)
        with mock.patch.object(self.window, "display") as counted:
            filters.s.setDate(QDate(2026, 1, 1))
            filters.s.setDate(QDate(2026, 1, 2))
            filters.by.setDate(QDate(2026, 1, 31))
            self.assertEqual(counted.call_count, 0)
            QTest.qWait(summary_filters.DATES_PAUSE_MS + 100)
        self.assertEqual(counted.call_count, 1)

    def test_sorting_chosen_by_user_stays_after_balance(self):
        table = self.window.by_meetings
        table.sortItems(5, Qt.SortOrder.AscendingOrder)
        self.window._show_balance(providers.Balance(8.52, 10.0, "ключ", 1.48, 2.0))
        self.window.filters._pick_project(None)
        header = table.horizontalHeader()
        self.assertEqual((header.sortIndicatorSection(), header.sortIndicatorOrder()), (5, Qt.SortOrder.AscendingOrder))
        self.assertEqual(table.item(table.rowCount() - 1, 0).text(), "Вне встреч")  # the largest sum is last

    def test_money_and_numbers_in_english(self):
        try:
            language.set_("en")
            self.assertEqual((catalog.money(1.594), catalog.money(0.004)), ("$1.59", "less than $0.01"))
            self.assertEqual((summary_report.number(0.8), summary_report.dollars("0.8")), ("0.8", "$0.8"))
            self.assertEqual(summary_report.or_dash(0.004), "< $0.01")
        finally:
            language.set_("ru")
        self.assertEqual((catalog.money(1.594), summary_report.number(1.5)), ("1,59 $", "1,5"))
        self.assertEqual(summary_report.or_dash(0.004), "< 0,01 $")

    def test_model_name_in_tooltips_is_text(self):
        folder = next(p for p in self.recordings.iterdir() if p.is_dir())
        catalog.set_(folder, minutes_cost=0.2, minutes_model="<b>my</b>-model")
        window = SummaryWindow(self.recordings, lambda: None, "OpenRouter", (["Склад"], {}))
        self.addCleanup(window.close)
        window.display()
        table = window.by_meetings
        tooltips = [table.item(r, 0).toolTip() for r in range(table.rowCount())]
        self.assertTrue(any("&lt;b&gt;my&lt;/b&gt;-model" in t for t in tooltips))
        grid = window._models
        names = [grid.itemAtPosition(r, 0).widget() for r in range(grid.rowCount()) if grid.itemAtPosition(r, 0)]
        self.assertTrue(all(n.textFormat() == Qt.TextFormat.PlainText for n in names))


if __name__ == "__main__":
    unittest.main()
