# SPDX-License-Identifier: GPL-3.0-only
"""Notepad during recording: the pencil on the recording bar asks for the notepad, lines get the recording minute, the
agenda comes with the project, Esc hides the notepad, notes are saved; in the meeting it is the "Notes" tab."""

import pathlib
import tempfile
import unittest

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from knotes import notes, catalog, projects, transcript
from knotes.ui.notepad import Notepad, NotepadWindow
from knotes.ui.recording_bar import RecordingBar

app = QApplication.instance() or QApplication([])


class TestNotepad(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.recordings = pathlib.Path(self._tmp.name)
        self.folder = self.recordings / "2026-10-23_10-00"
        self.folder.mkdir()
        self.seconds = 0.0

    def tearDown(self):
        app.processEvents()
        self._tmp.cleanup()

    def _window(self, window: object) -> object:
        """Windows are deleted after the test while Qt is still running: otherwise Python deletes them after Qt."""
        self.addCleanup(app.processEvents)
        self.addCleanup(window.deleteLater)
        return window

    def test_button_asks_notepad_and_tooltip_with_hotkey(self):
        line = self._window(RecordingBar(False, "Ctrl+Alt+R", "Ctrl+Alt+M", "Ctrl+Alt+N"))
        requests: list[bool] = []
        line.note.connect(lambda: requests.append(True))
        line._note.click()
        self.assertEqual(requests, [True])
        self.assertIn("Ctrl+Alt+N", line._note.toolTip())
        line.collapse(True)
        self.assertFalse(line._note.isVisibleTo(line))  # a collapsed bar has only the dot and the time
        line.close()

    def test_lines_get_recording_minute_and_are_saved(self):
        window = self._window(NotepadWindow(self.folder, False, ["Склад"], lambda: self.seconds))
        window.show()
        editor = window.notepad.editor
        self.seconds = 121.4
        editor.insertPlainText("Решение - цена из прайса")  # QTest.keyClicks cannot type Cyrillic
        QTest.keyClick(editor, Qt.Key.Key_Return)
        self.seconds = 300
        editor.insertPlainText("спросить про остановку?")
        QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)  # checkbox for the line
        window.hide_away()
        self.assertFalse(window.isVisible())
        self.assertEqual(
            notes.with_minutes(self.folder), [(121, "Решение - цена из прайса"), (300, "- [ ] спросить про остановку?")]
        )

    def test_project_agenda_on_project_choice(self):
        projects.set_for_meeting(self.recordings, "Склад", "Сроки препрода\nКто подписывает акты")
        window = self._window(NotepadWindow(self.folder, False, ["Склад"], lambda: self.seconds))
        window.select_project("Склад")
        self.assertEqual(catalog.properties(self.folder)["проект"], "Склад")
        self.assertEqual([p.text for p in window.notepad.agenda.items()], ["Сроки препрода", "Кто подписывает акты"])
        window.notepad.agenda.list_.item(0).setCheckState(Qt.CheckState.Checked)
        window.notepad.save()
        read_count = notes.read(self.folder)
        self.assertEqual(read_count.agenda[0], notes.AgendaItem("Сроки препрода", True))
        notes.close_agenda(self.recordings, "Склад", read_count)
        self.assertEqual(projects.for_meeting(self.recordings, "Склад"), "Кто подписывает акты")
        window.close()

    def test_enter_at_line_start_and_checkbox_on_long_line(self):
        window = self._window(NotepadWindow(self.folder, False, [], lambda: self.seconds))
        window.resize(380, 400)
        window.show()
        editor = window.notepad.editor
        self.seconds = 10
        editor.insertPlainText("abc")
        editor.moveCursor(QTextCursor.MoveOperation.StartOfBlock)
        self.seconds = 100
        QTest.keyClick(editor, Qt.Key.Key_Return)
        lines = editor.lines()
        self.assertEqual([(s.text, s.second) for s in lines], [("", None), ("abc", 10)])  # the minute moved with it
        editor.moveCursor(QTextCursor.MoveOperation.End)
        editor.insertPlainText(" очень длинная строка" * 12)
        QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(editor.lines()[1].text.startswith("- [ ] abc"))  # checkbox for the whole line, not mid-line
        window.close()

    def test_calendar_project_keeps_own_agenda_items(self):
        projects.set_for_meeting(self.recordings, "Склад", "Сроки препрода")
        window = self._window(NotepadWindow(self.folder, False, ["Склад"], lambda: self.seconds))
        window.notepad.agenda.new.setText("Свой вопрос")
        window.notepad.agenda._add()
        catalog.set_(self.folder, project="Склад")  # the calendar has already written the meeting project
        window.select_project("Склад")
        self.assertEqual([p.text for p in window.notepad.agenda.items()], ["Свой вопрос", "Сроки препрода"])
        window.select_project("Склад")  # the same project again: nothing changes
        self.assertEqual(len(window.notepad.agenda.items()), 2)
        window.close()

    def test_notes_tab_does_not_overwrite_recording_notepad(self):
        """The meeting was opened during recording (the tab is empty), notes are written in the notepad over the
        windows: closing the tab without edits does not delete them."""
        tab = self._window(Notepad(self.folder))
        notes.add(self.folder, 30, "из блокнота записи")
        tab.save()
        self.assertEqual(notes.with_minutes(self.folder), [(30, "из блокнота записи")])
        tab.reload_from_disk()
        self.assertEqual(tab.editor.toPlainText(), "из блокнота записи")
        tab.editor.setPlainText("правка во вкладке")
        tab.save()
        self.assertEqual(notes.read(self.folder).lines[0].text, "правка во вкладке")

    def test_project_page_does_not_return_discussed_and_old_name(self):
        from knotes.ui.project_page import ProjectPage

        projects.set_for_meeting(self.recordings, "Склад", "Сроки\nАкты")
        page = self._window(ProjectPage(self.recordings, "Склад"))
        notes.close_agenda(self.recordings, "Склад", notes.Notes((notes.AgendaItem("Сроки", True),)))
        page.close()  # nothing was edited: the file is not overwritten with the old text
        self.assertEqual(projects.for_meeting(self.recordings, "Склад"), "Акты")
        another = self._window(ProjectPage(self.recordings, "Склад"))
        projects.rename(self.recordings, "Склад", "Склад 2")
        another.close_without_saving()
        self.assertEqual(projects.for_meeting(self.recordings, "Склад"), "")  # the old name did not come back
        self.assertEqual(projects.for_meeting(self.recordings, "Склад 2"), "Акты")

    def test_minutes_open_the_place_in_meeting(self):
        transcript.save(
            self.folder,
            [{"начало": 5.0, "конец": 9.0, "кто": "Я", "текст": "Смотрим."}],
            {"длительность": 600, "провайдер": "x", "стоимость": 0.1},
            [],
        )
        notes.add(self.folder, 65, "цена из прайса")
        notepad = self._window(Notepad(self.folder))
        places: list[float] = []
        notepad.to_place.connect(places.append)
        notepad.resize(500, 300)
        notepad.show()
        app.processEvents()
        field = notepad.editor.field
        top = field._lines()[0][1]
        QTest.mouseClick(field, Qt.MouseButton.LeftButton, pos=QPoint(10, top + 4))
        self.assertEqual(places, [65.0])
        notepad.close()


if __name__ == "__main__":
    unittest.main()
