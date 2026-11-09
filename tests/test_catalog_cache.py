# SPDX-License-Identifier: GPL-3.0-only
"""The window refreshes the meeting list every few seconds: the price, the dates and the search do not re-read
unchanged files, a broken answer or a vanished folder does not break the refresh."""

import datetime
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from knotes import ai_tools, catalog, recognition, settings, transcript

INFO = {"длительность": 600, "провайдер": "OpenRouter, elevenlabs/scribe-v2", "стоимость": 0.1}


class TestCatalogCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.recordings = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _ready(self, name: str, text: str = "Обсудили бюджет") -> pathlib.Path:
        folder = self.recordings / name
        folder.mkdir()
        transcript.save(folder, [{"начало": 1.0, "конец": 2.0, "кто": "Я", "текст": text}], INFO, [])
        return folder

    def _answers(self, folder: pathlib.Path, parts: list) -> None:
        (folder / transcript.ANSWERS).write_text(json.dumps({"общая": parts}), encoding="utf-8")

    def test_file_meeting_keeps_its_date_when_the_file_is_out_of_reach(self):
        """The date of a meeting from a ready file is noted when the file is taken in: later the list does not look
        for the file (it may be on a network drive that is off)."""
        source = pathlib.Path(self._tmp.name) / "внешний" / "созвон.mp4"
        source.parent.mkdir()
        source.write_bytes(b"0")
        when = datetime.datetime(2026, 10, 1, 9, 30).timestamp()
        os.utime(source, (when, when))
        folder = recognition.folder_for_file(source, self.recordings)
        source.unlink()  # the network drive is off
        self.assertEqual(catalog.folder_date(folder), datetime.datetime(2026, 10, 1, 9, 30))

    def test_price_reads_answers_once_until_they_change(self):
        folder = self._ready("2026-10-22_10-00")
        self._answers(folder, [{"ответ": {"usage": {"seconds": 100, "cost": 0.01}}}])
        meetings = catalog.list_(self.recordings)
        with mock.patch.object(catalog.transcript, "read_json", wraps=catalog.transcript.read_json) as read:
            self.assertAlmostEqual(catalog.price_per_second(meetings), 0.0001)
            self.assertAlmostEqual(catalog.price_per_second(meetings), 0.0001)
            self.assertEqual(read.call_count, 1)
            self._answers(folder, [{"ответ": {"usage": {"seconds": 100, "cost": 0.02}}}, {"ответ": None}])
            self.assertAlmostEqual(catalog.price_per_second(meetings), 0.0002)
            self.assertEqual(read.call_count, 2)

    def test_broken_answer_is_skipped_others_count(self):
        first, second = self._ready("2026-10-22_10-00"), self._ready("2026-10-22_11-00")
        self._answers(first, ["не ответ", {"ответ": {"usage": {"seconds": "много", "cost": 1}}}])
        self._answers(second, [{"ответ": {"usage": {"seconds": 200, "cost": 0.04}}}])
        with self.assertLogs("knotes.catalog", "WARNING"):
            self.assertAlmostEqual(catalog.price_per_second(catalog.list_(self.recordings)), 0.0002)

    def test_answers_of_wrong_shape_are_skipped(self):
        folder = self._ready("2026-10-22_10-00")
        (folder / transcript.ANSWERS).write_text('{"общая": 5}', encoding="utf-8")
        with self.assertLogs("knotes.catalog", "WARNING"):
            self.assertIsNone(catalog.price_per_second(catalog.list_(self.recordings)))

    def test_date_of_meeting_from_file_is_read_once(self):
        source = self.recordings / "Созвон с клиентом.mp4"
        source.write_bytes(b"x")
        os.utime(source, (1761100000, 1761100000))
        folder = self.recordings / "Созвон с клиентом"
        folder.mkdir()
        (folder / transcript.SOURCE).write_text(str(source), encoding="utf-8")
        with mock.patch.object(catalog, "folder_date", wraps=catalog.folder_date) as date:
            found = catalog.meeting(folder)
            for _ in range(3):
                self.assertEqual(found.date.timestamp(), 1761100000)
                self.assertEqual(found.heading, "Созвон с клиентом")
            self.assertEqual(date.call_count, 1)

    def test_list_sorts_without_rereading_dates(self):
        self._ready("2026-10-22_10-00")
        (self.recordings / "Запись.mp3").write_bytes(b"x")
        catalog.list_(self.recordings)
        with mock.patch.object(catalog, "folder_date", wraps=catalog.folder_date) as date:
            meetings = catalog.list_(self.recordings)
            self.assertEqual(date.call_count, 1)  # only the loose file, the meeting comes from the cache
            [v.date for v in meetings for _ in range(3)]
            self.assertEqual(date.call_count, 1)

    def test_unreadable_transcript_does_not_break_search(self):
        folder = self._ready("2026-10-22_10-00", "Обсудили ЕГАИС")
        meetings = catalog.list_(self.recordings)
        (folder / transcript.TRANSCRIPT).unlink()
        with self.assertLogs("knotes.catalog", "WARNING"):
            self.assertEqual(catalog.find(meetings, "егаис"), {})

    def test_vanished_folder_is_logged(self):
        self._ready("2026-10-22_10-00")
        with (
            mock.patch.object(catalog, "_row_label", side_effect=FileNotFoundError(2, "gone")),
            self.assertLogs("knotes.catalog", "WARNING") as log,
        ):
            self.assertEqual(catalog.list_(self.recordings), [])
        self.assertIn("2026-10-22_10-00", "\n".join(log.output))

    def test_search_reads_transcript_once_until_it_changes(self):
        folder = self._ready("2026-10-22_10-00", "Обсудили ЕГАИС")
        meetings = catalog.list_(self.recordings)
        real = pathlib.Path.read_text
        reads = []

        def counted(path: pathlib.Path, *arguments: object, **named: object) -> str:
            reads.append(path.name)
            return real(path, *arguments, **named)

        with mock.patch.object(pathlib.Path, "read_text", autospec=True, side_effect=counted):
            self.assertEqual(catalog.find(meetings, "егаис"), {folder: 1})
            self.assertEqual(catalog.find(meetings, "ЕГАИС "), {folder: 1})
            self.assertEqual(catalog.find(meetings, "бюджет"), {})
            self.assertEqual(reads.count(transcript.TRANSCRIPT), 2)  # once per query
            transcript.fix_phrase(folder, 0, "Обсудили бюджет и бюджет ЕГАИС")
            self.assertEqual(catalog.find(meetings, "бюджет"), {folder: 2})


class TestSearchTool(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.recordings = pathlib.Path(self._tmp.name)
        patcher = mock.patch.object(
            settings, "load", return_value=settings.Settings(recordings_folder=str(self.recordings))
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_limit_before_building_answer(self):
        for hour in range(10, 15):
            folder = self.recordings / f"2026-10-22_{hour}-00"
            folder.mkdir()
            transcript.save(folder, [{"начало": 1.0, "конец": 2.0, "кто": "Я", "текст": "Про склад"}], INFO, [])
        with mock.patch.object(ai_tools, "_card", wraps=ai_tools._card) as card:
            result = ai_tools.search_meetings("склад", limit=2)
        self.assertEqual(card.call_count, 2)
        self.assertEqual([v["встреча"] for v in result["найдено"]], ["2026-10-22_14-00", "2026-10-22_13-00"])
        self.assertEqual(result["встреч_найдено"], 5)


if __name__ == "__main__":
    unittest.main()
