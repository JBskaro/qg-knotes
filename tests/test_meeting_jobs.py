# SPDX-License-Identifier: GPL-3.0-only
"""Background jobs on meetings: what each meeting is busy with, one recognition at a time, the minutes alongside,
progress at most once a second, a failure kept in the meeting."""

import errno
import pathlib
import tempfile
import threading
import time
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from knotes import catalog, meeting_jobs, recognition, screen, transcript
from knotes.meeting_list import Busy

qt = QApplication.instance() or QApplication([])


def wait_for(condition: object, seconds: float = 5.0) -> None:
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        qt.processEvents()
        time.sleep(0.01)


class TestJobs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.recordings = pathlib.Path(temporary.name)
        self.folder = self.recordings / "2026-10-27_10-00"
        self.folder.mkdir()
        self.jobs = meeting_jobs.Jobs()
        self.addCleanup(self.jobs.pool.waitForDone, 5000)
        self.addCleanup(self.jobs.minutes_pool.waitForDone, 5000)
        self.results: list[tuple] = []
        self.jobs.transcribed.connect(lambda *result: self.results.append(result))
        self.params = mock.Mock()

    def test_one_recognition_of_a_meeting_at_a_time(self):
        """The meeting is busy while it is transcribed; the same meeting asked again is not sent twice; the result
        comes with the meeting no longer busy."""
        gate, seen = threading.Event(), []

        def recognize(folder: pathlib.Path, params: object, how: tuple, progress: object) -> pathlib.Path:
            seen.append((folder, params, how))
            gate.wait(5)
            return folder / transcript.TRANSCRIPT

        with mock.patch.object(meeting_jobs, "recognize_folder", side_effect=recognize):
            self.assertTrue(self.jobs.recognize(self.folder, self.params, again=True, delete_after=True))
            self.assertFalse(self.jobs.recognize(self.folder, self.params))
            self.assertEqual(self.jobs.busy, {self.folder: Busy.TRANSCRIBING})
            gate.set()
            wait_for(lambda: self.results)
        self.assertEqual(seen, [(self.folder, self.params, (True, True))])
        self.assertEqual(self.results, [(self.folder, self.folder / transcript.TRANSCRIPT)])
        self.assertEqual(self.jobs.busy, {})

    def test_failure_stays_in_the_meeting(self):
        """A failed recognition is written in the meeting, so the list shows it with Retry."""
        full = OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(meeting_jobs, "recognize_folder", side_effect=full), self.assertLogs(level="ERROR"):
            self.jobs.recognize(self.folder, self.params)
            wait_for(lambda: self.results)
        self.assertIs(self.results[0][1], full)
        self.assertIn("закончилось место", (self.folder / catalog.ERROR).read_text(encoding="utf-8"))
        self.assertEqual(self.jobs.busy, {})

    def test_file_becomes_a_meeting(self):
        file = self.recordings.parent / f"{self.recordings.name}.mp4"
        file.write_bytes(b"x")
        self.addCleanup(file.unlink)
        taken = self.recordings / file.stem
        with (
            mock.patch.object(meeting_jobs.recognition, "from_file", return_value=taken) as copied,
            mock.patch.object(meeting_jobs, "recognize_folder", return_value=taken / transcript.TRANSCRIPT) as sent,
        ):
            self.jobs.take_file(file, self.recordings, self.params, False)
            self.assertEqual(self.jobs.busy, {taken: Busy.TRANSCRIBING})
            wait_for(lambda: self.results)
        copied.assert_called_once_with(file, self.recordings)
        self.assertEqual(sent.call_args.args[:3], (taken, self.params, (False, False)))
        self.assertEqual(self.results, [(taken, taken / transcript.TRANSCRIPT)])

    def test_interrupted_recordings_are_assembled(self):
        """A cut-off recording is restoring until it is assembled; one with only the video left says so."""
        (self.folder / transcript.MANIFEST).write_text("{}", encoding="utf-8")
        video_only = self.recordings / "2026-10-27_11-00"
        video_only.mkdir()
        (video_only / screen.CHUNK_LIST).write_text("", encoding="utf-8")
        restored: list[tuple] = []
        self.jobs.restored.connect(lambda *result: restored.append(result))
        broken = OSError(errno.EIO, "I/O error")
        with (
            mock.patch.object(meeting_jobs.recording, "collect", side_effect=["", broken]),
            self.assertLogs(level="WARNING") as logged,
        ):
            self.assertEqual(self.jobs.restore(self.recordings), [self.folder, video_only])
            self.assertEqual(set(self.jobs.busy.values()), {Busy.RESTORING})
            wait_for(lambda: len(restored) == 2)
        self.assertEqual(restored, [(self.folder, True, ""), (video_only, False, broken)])
        self.assertTrue(any("was not assembled" in line for line in logged.output))
        self.assertEqual(self.jobs.busy, {})
        self.assertEqual(self.jobs.restore(self.recordings / "нет такой"), [])

    def test_minutes_alongside(self):
        written: list[tuple] = []
        self.jobs.minutes_written.connect(lambda *result: written.append(result))
        with mock.patch.object(meeting_jobs.minutes, "compose", return_value=self.folder / "протокол.md") as composed:
            self.jobs.compose(self.folder, self.params)
            self.assertEqual(self.jobs.minutes, {self.folder})
            wait_for(lambda: written)
        composed.assert_called_once_with(self.folder, self.params)
        self.assertEqual(written, [(self.folder, self.folder / "протокол.md")])
        self.assertEqual(self.jobs.minutes, set())

    def test_progress_shown_at_most_once_a_second(self):
        """Local recognition reports every part: the busy row changes at once and then at most once a second, from
        when the first part was done."""
        shown: list[tuple] = []
        self.jobs.progress_shown.connect(lambda folder, progress: shown.append((folder, progress)))
        report = self.jobs._progress_for(self.folder)
        report(0, 10)
        start = self.jobs.progress[self.folder].start
        for done in range(1, 6):
            report(done, 10)
        self.assertEqual(len(shown), 1)
        self.jobs._progress_later.timeout.emit()  # a second later
        self.assertEqual([p.done_count for _, p in shown], [0, 5])
        self.assertEqual(shown[-1][1].start, start)
        self.jobs._progress_later.timeout.emit()  # nothing new
        self.assertEqual(len(shown), 2)


class TestRecognizeFolder(unittest.TestCase):
    def test_audio_deleted_after_when_asked(self):
        with tempfile.TemporaryDirectory() as name:
            folder = pathlib.Path(name)
            for file in (*(f"{track}.flac" for track in recognition.TRACKS), screen.VIDEO_FILE, catalog.ERROR):
                (folder / file).write_bytes(b"x")
            with mock.patch.object(meeting_jobs.recognition, "process", return_value=folder / "р.md") as process:
                self.assertEqual(
                    meeting_jobs.recognize_folder(folder, mock.Mock(), (True, True), print), folder / "р.md"
                )
            self.assertTrue(process.call_args.args[2])  # again
            self.assertEqual(list(folder.iterdir()), [])  # the old error goes before recognition
            (folder / "микрофон.flac").write_bytes(b"x")
            with mock.patch.object(meeting_jobs.recognition, "process", return_value=folder / "р.md"):
                meeting_jobs.recognize_folder(folder, mock.Mock(), (False, False), print)
            self.assertTrue((folder / "микрофон.flac").exists())


if __name__ == "__main__":
    unittest.main()
