# SPDX-License-Identifier: GPL-3.0-only
"""Rows of the meeting list: state captions, progress, sending price, the project and the search."""

import pathlib
import tempfile
import unittest

from knotes import catalog, meeting_rows
from knotes.meeting_list import Busy
from knotes.meeting_rows import MeetingItem, Progress


class TestMeetingRows(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)

    def _meeting(self, name: str, status: str, project: str = "", seconds: float = 0) -> catalog.Meeting:
        return catalog.Meeting(self.root / name, status, seconds=seconds, project=project)

    def test_progress_left(self):
        self.assertEqual(Progress(0, 4, 0.0).remaining(100.0), "")
        self.assertEqual(Progress(4, 4, 0.0).remaining(100.0), "")
        self.assertEqual(Progress(1, 4, 0.0).remaining(120.0), "осталось около 6 мин")
        self.assertEqual(Progress(3, 4, 0.0).remaining(1.0), "осталось около 1 мин")  # never "0 min"

    def test_state_caption_and_color(self):
        ready = self._meeting("а", catalog.READY)
        self.assertEqual(MeetingItem(ready).status, ("Готова", "готова"))
        self.assertEqual(MeetingItem(self._meeting("б", "непонятно")).status, ("непонятно", "ждет"))
        self.assertEqual(MeetingItem(ready, Busy.TRANSCRIBING).status, ("Распознается", "идет"))
        busy = MeetingItem(ready, Busy.TRANSCRIBING, Progress(2, 5, 0.0))
        self.assertEqual(busy.status, ("Распознается, 2 из 5", "идет"))

    def test_recognizable_only_with_audio_and_not_busy(self):
        recorded = self._meeting("а", catalog.RECORDED)
        recorded.folder.mkdir()
        self.assertFalse(MeetingItem(recorded).recognizable)
        (recorded.folder / "микрофон.flac").write_bytes(b"x")
        self.assertTrue(MeetingItem(recorded).recognizable)
        self.assertFalse(MeetingItem(recorded, Busy.RECORDING).recognizable)
        self.assertTrue(MeetingItem(self._meeting("файл.mp4", catalog.FILE)).recognizable)
        self.assertFalse(MeetingItem(self._meeting("б", catalog.INTERRUPTED)).recognizable)

    def test_rows_with_busy_progress_and_price(self):
        """Both tracks of an own recording are paid for; a meeting without its length has no price."""
        first, second = self._meeting("а", catalog.RECORDED, seconds=100), self._meeting("б", catalog.RECORDED)
        progress = Progress(1, 3, 0.0)
        rows = meeting_rows.items([first, second], {first.folder: Busy.TRANSCRIBING}, {first.folder: progress}, 0.001)
        self.assertEqual(
            [(v.occupied, v.progress, v.price) for v in rows], [(Busy.TRANSCRIBING, progress, 0.2), (None,) * 3]
        )
        self.assertEqual([v.price for v in meeting_rows.items([first], {}, {}, None)], [None])
        later = Progress(2, 3, 0.0)
        self.assertEqual([v.progress for v in meeting_rows.with_progress(rows, first.folder, later)], [later, None])

    def test_project_and_search(self):
        rows = [
            MeetingItem(self._meeting("а", catalog.READY, "Склад")),
            MeetingItem(self._meeting("б", catalog.READY)),
            MeetingItem(self._meeting("в", catalog.READY, "Склад")),
        ]
        self.assertEqual(meeting_rows.of_project(rows, None), rows)
        self.assertEqual(meeting_rows.of_project(rows, ""), [rows[1]])
        in_project = meeting_rows.of_project(rows, "Склад")
        self.assertEqual(in_project, [rows[0], rows[2]])
        found = meeting_rows.found_only(in_project, {rows[2].meeting.folder: 3})
        self.assertEqual([(v.meeting.folder.name, v.found) for v in found], [("в", 3)])


if __name__ == "__main__":
    unittest.main()
