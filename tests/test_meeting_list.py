# SPDX-License-Identifier: GPL-3.0-only
"""What the meeting list shows: read in the background, the minutes summary and search matches read once per
version of a file, an unreadable file costs only its own row the detail."""

import pathlib
import tempfile
import unittest
from unittest import mock

from knotes import catalog, language, meeting_list, minutes, tasks, transcript

MINUTES = "# Итоги\n\n## Решения\n\n1. А.\n2. Б.\n\n## Задачи\n\n1. Сделать. Игорь.\n"


class TestMeetingList(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.recordings = pathlib.Path(self._temporary.name)
        self.first = self._meeting("2026-10-22_10-00", "Обсудили ёлку и елку")
        self.second = self._meeting("2026-10-22_11-00", "Про бюджет")

    def tearDown(self):
        self._temporary.cleanup()

    def _meeting(self, name: str, text: str) -> pathlib.Path:
        folder = self.recordings / name
        folder.mkdir()
        transcript.save(
            folder,
            [{"начало": 1.0, "конец": 2.0, "кто": "Я", "текст": text}],
            {"длительность": 60, "провайдер": "OpenRouter", "стоимость": 0.1},
            [],
        )
        return folder

    def test_recordings_folder_walked_once_per_reading(self):
        """The list and the open task count come from one walk of the folder: it is read every 5 seconds."""
        with mock.patch.object(catalog, "list_", wraps=catalog.list_) as listed:
            meeting_list.read(self.recordings, False)
        self.assertEqual(listed.call_count, 1)

    def test_busy_states_shown_in_the_interface_language(self):
        self.assertEqual(meeting_list.Busy.TRANSCRIBING.caption, "Распознается")
        try:
            language.set_("en")
            self.assertEqual(meeting_list.Busy.TRANSCRIBING.caption, "Transcribing")
        finally:
            language.set_("ru")

    def test_minutes_summary_read_once_per_version(self):
        (self.first / minutes.FILE).write_text(MINUTES, encoding="utf-8")
        with mock.patch.object(meeting_list.minutes, "result", wraps=minutes.result) as read:
            self.assertEqual(meeting_list.minutes_result(self.first), "2 решения · 1 задача")
            self.assertEqual(meeting_list.minutes_result(self.first), "2 решения · 1 задача")
            self.assertEqual(read.call_count, 1)
            (self.first / minutes.FILE).write_text(MINUTES + "2. Еще. Анна.\n", encoding="utf-8")
            self.assertEqual(meeting_list.minutes_result(self.first), "2 решения · 2 задачи")
        self.assertEqual(meeting_list.minutes_result(self.second), "")  # no minutes

    def test_unreadable_minutes_give_no_summary(self):
        """Minutes saved in ANSI by another editor: the row goes without the summary, the list is not cut off."""
        (self.first / minutes.FILE).write_bytes("# Итоги\n\n## Решения\n\n1. Цена.\n".encode("cp1251"))
        with self.assertLogs(meeting_list.log, "WARNING"):
            self.assertEqual(meeting_list.minutes_result(self.first), "")

    def test_search_reads_a_transcript_again_only_when_it_changed(self):
        meetings = catalog.list_(self.recordings)
        with mock.patch.object(meeting_list.catalog, "find", wraps=catalog.find) as found:
            self.assertEqual(meeting_list.find(meetings, "елку"), {self.first: 2})
            self.assertEqual(meeting_list.find(meetings, "елку"), {self.first: 2})
            self.assertEqual(found.call_count, 2)  # once per meeting
            (self.second / transcript.TRANSCRIPT).write_text("И тут елку\n", encoding="utf-8")
            self.assertEqual(meeting_list.find(meetings, "елку"), {self.first: 2, self.second: 1})
            self.assertEqual(found.call_count, 3)
            self.assertEqual(meeting_list.find(meetings, "бюджет"), {})

    def test_unreadable_transcript_is_searched_by_title(self):
        (self.first / transcript.TRANSCRIPT).write_bytes("Обсудили ёлку".encode("cp1251"))
        catalog.set_(self.first, title="Ёлка во дворе")
        meetings = catalog.list_(self.recordings)
        with self.assertLogs(meeting_list.log, "WARNING"):
            self.assertEqual(meeting_list.find(meetings, "елка"), {self.first: 1})

    def test_search_pattern_as_in_the_list(self):
        pattern = meeting_list.search_pattern("елку")
        self.assertEqual(pattern.split("Нарядили ЁЛКУ и елку"), ["Нарядили ", "ЁЛКУ", " и ", "елку", ""])
        self.assertEqual(meeting_list.search_pattern("a.b").findall("a.b axb"), ["a.b"])

    def test_everything_the_list_shows_in_one_reading(self):
        (self.first / minutes.FILE).write_text(MINUTES, encoding="utf-8")
        snapshot = meeting_list.read(self.recordings, priced=False)
        self.assertEqual([v.folder for v in snapshot.meetings], [self.second, self.first])
        self.assertIsNone(snapshot.price)
        self.assertEqual(snapshot.open_tasks, 1)

    def test_board_that_cannot_be_read_keeps_the_list(self):
        with (
            mock.patch.object(meeting_list.tasks, "tasks", side_effect=UnicodeDecodeError("utf-8", b"", 0, 1, "x")),
            self.assertLogs(meeting_list.log, "WARNING"),
        ):
            snapshot = meeting_list.read(self.recordings, priced=True)
        self.assertEqual(len(snapshot.meetings), 2)
        self.assertIsNone(snapshot.open_tasks)
        self.assertIs(meeting_list.tasks, tasks)


if __name__ == "__main__":
    unittest.main()
