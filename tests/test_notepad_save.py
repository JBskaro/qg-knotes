# SPDX-License-Identifier: GPL-3.0-only
"""Notes that could not be saved (the file is busy, the disk is full) stay in the notepad with the reason and a copy
outside the meeting folder; the recording notepad does not hide while its notes are not saved."""

import errno
import pathlib
import tempfile
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from knotes import failures, notes
from knotes import settings
from knotes.language import tr
from knotes.ui import notepad as notepad_module
from knotes.ui.notepad import Notepad, NotepadWindow

app = QApplication.instance() or QApplication([])


class TestSaveFailure(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name) / "2026-10-26_10-00"
        self.folder.mkdir()

    def tearDown(self):
        app.processEvents()
        self._tmp.cleanup()

    def test_text_stays_with_reason_and_a_copy_and_saves_later(self):
        """The meeting window closes even when its notes did not save: the copy keeps them."""
        spare = pathlib.Path(self._tmp.name) / "настройки"
        notepad = Notepad(self.folder)
        self.addCleanup(notepad.deleteLater)
        notepad.editor.setPlainText("Решение - цена из прайса")
        busy = PermissionError(errno.EACCES, "Permission denied", str(self.folder / notes.FILE))
        with (
            mock.patch.object(notes, "write", side_effect=busy),
            mock.patch.object(settings, "data_folder", return_value=spare),
            self.assertLogs("knotes.ui.notepad", "WARNING"),
        ):
            self.assertFalse(notepad.save())
        copy_ = spare / notepad_module.SPARE_FOLDER / f"{self.folder.name}.md"
        self.assertEqual(copy_.read_text(encoding="utf-8"), "Решение - цена из прайса\n")
        self.assertEqual(
            notepad.failure.text(),
            tr("{reason} A copy of the notes: {copy_}", reason=failures.text(busy), copy_=copy_),
        )
        self.assertTrue(notepad.failure.isVisibleTo(notepad))
        self.assertEqual(notepad.editor.toPlainText(), "Решение - цена из прайса")
        self.assertTrue(notepad.save())  # the next try (an edit, closing) writes it
        self.assertFalse(notepad.failure.isVisibleTo(notepad))
        self.assertFalse(copy_.exists())
        self.assertEqual([s.text for s in notes.read(self.folder).lines], ["Решение - цена из прайса"])
        self.assertTrue(notepad.save())  # nothing changed: nothing to write

    def test_disk_full_everywhere_reason_without_copy(self):
        notepad = Notepad(self.folder)
        self.addCleanup(notepad.deleteLater)
        notepad.editor.setPlainText("важное")
        full = OSError(errno.ENOSPC, "No space left on device")
        with (
            mock.patch.object(notes, "write", side_effect=full),
            mock.patch.object(pathlib.Path, "write_text", side_effect=full),
            mock.patch.object(settings, "data_folder", return_value=pathlib.Path(self._tmp.name)),
            self.assertLogs("knotes.ui.notepad", "WARNING") as logged,
        ):
            self.assertFalse(notepad.save())
        self.assertEqual(len(logged.records), 2)
        self.assertEqual(notepad.failure.text(), failures.text(full))
        self.assertEqual(notepad.editor.toPlainText(), "важное")

    def test_notepad_window_stays_open_when_notes_are_not_saved(self):
        window = NotepadWindow(self.folder, False, [], lambda: 0.0)
        self.addCleanup(window.deleteLater)
        window.show()
        window.notepad.editor.setPlainText("важное")
        busy = PermissionError(errno.EACCES, "занят", str(self.folder / notes.FILE))
        with (
            mock.patch.object(notes, "write", side_effect=busy),
            mock.patch.object(settings, "data_folder", return_value=pathlib.Path(self._tmp.name)),
            self.assertLogs("knotes.ui.notepad", "WARNING"),
        ):
            window.hide_away()
        self.assertTrue(window.isVisible())
        window.hide_away()
        self.assertFalse(window.isVisible())


if __name__ == "__main__":
    unittest.main()
