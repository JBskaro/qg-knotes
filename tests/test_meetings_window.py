# SPDX-License-Identifier: GPL-3.0-only
"""Meetings list: a click on a ready meeting opens it, the "Minutes" button, the minutes result in the row, no
"Ready"."""

import dataclasses
import datetime
import errno
import pathlib
import tempfile
import unittest
from unittest import mock

import shiboken6
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QResizeEvent, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QLabel, QMenu, QMessageBox, QPushButton, QWidget

from knotes import catalog, projects, minutes, notebook, notes, transcript, kinds, meeting_rows, tasks
from knotes.meeting_list import Busy
from knotes.ui import help_page, meetings_window, meeting_view, projects_panel
from knotes.ui import style
from knotes.ui.meetings_window import MeetingsWindow
from knotes.ui.meeting_view import MeetingView
from knotes.ui.notebook_editor import BOLD, NUMBER, link_url
from knotes.ui.notebook_page import NotebookPage
from knotes.ui.tasks_window import TasksWindow

app = QApplication.instance() or QApplication([])
MINUTES = "# Итоги\n\n## Решения\n\n1. А.\n2. Б.\n3. В.\n\n## Задачи\n\n1. Г.\n\n## Открытые вопросы\n\n1. Д.\n2. Е.\n"


class TestMeetingsList(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.recordings = pathlib.Path(self._temporary.name)
        self.ready_one = self._meeting("2026-10-22_10-00", MINUTES)
        self.no_minutes = self._meeting("2026-10-22_11-00")
        self.recorded = self.recordings / "2026-10-22_12-00"
        self.recorded.mkdir()
        (self.recorded / "микрофон.flac").write_bytes(b"x")
        self.window = MeetingsWindow()
        self.opened_all: list[pathlib.Path] = []
        self.window.open_meeting.connect(self.opened_all.append)
        self.window.set_projects(["Склад"])
        self.window.show_meetings(catalog.list_(self.recordings), {}, {}, None)
        self.window.show()
        app.processEvents()

    def tearDown(self):
        self.window.close()
        self._temporary.cleanup()

    def test_update_button_only_when_a_release_is_available(self):
        self.assertFalse(self.window.update_button.isVisible())
        clicks = []
        self.window.update_requested.connect(lambda: clicks.append(True))
        self.window.show_update("99.0.0")
        self.assertTrue(self.window.update_button.isVisible())
        self.assertFalse(self.window.update_button.icon().isNull())
        self.assertEqual(self.window.update_button.text(), "")
        self.assertEqual(self.window.update_button.toolTip(), "Доступно обновление")
        self.assertIs(self.window.update_button.parentWidget(), self.window.projects_column)
        self.assertLess(
            self.window.update_button.y(), self.window.projects_column.element("настройки").listWidget().y()
        )
        self.window.update_button.click()
        self.assertEqual(clicks, [True])
        self.window.show_update("")
        self.assertFalse(self.window.update_button.isVisible())

    def test_global_search_opens_note_matches_and_updates_after_edit(self):
        opened = []
        self.window.find_notes.connect(lambda folder, query: opened.append((folder, query)))
        notes.write(self.recorded, notes.Notes(lines=(notes.NoteLine("Личная ёлка", private=True),)))
        self.window.search.setText("елка")
        self.window._show_items()
        self.window._search_delay.stop()
        self._wait_for_search()
        item = self.window.results.item(0)
        QTest.mouseClick(
            self.window.results.viewport(),
            Qt.MouseButton.LeftButton,
            pos=self.window.results.visualItemRect(item).center(),
        )
        self.assertEqual(opened, [(self.recorded, "елка")])
        notes.write(self.recorded, notes.Notes(lines=(notes.NoteLine("Другой текст"),)))
        self.window.refresh_notes_search()
        self.window._show_items()
        self._wait_for_search()
        self.assertEqual(self.window.results.count(), 0)
        self.window._search_delay.stop()

    def test_new_search_supersedes_pending_query_and_finds_minutes_and_notebook(self):
        saved = notebook.save(self.recordings, notebook.Note(title="Решения в блокноте"), notebook.Note())
        jobs = []
        with mock.patch(
            "knotes.ui.archive_results.run_in_background", side_effect=lambda p, work, done: jobs.append((work, done))
        ):
            for query in ("Начнем", "Решения"):
                self.window.search.setText(query)
                self.window._search_delay.stop()
                self.window._show_items()
            self.assertEqual(len(jobs), 1)
            jobs[0][1](jobs[0][0]())
            self.assertEqual(self.window.results.count(), 0)
            jobs[1][1](jobs[1][0]())
        results = self.window.results
        hits = [results.item(i).data(Qt.ItemDataRole.UserRole) for i in range(results.count())]
        self.assertEqual({(h.kind, h.key) for h in hits}, {("notebook", saved.id), ("minutes", self.ready_one.name)})
        self.assertTrue(all("Решения" in h.excerpt for h in hits))

    def _wait_for_search(self):
        for _ in range(200):
            app.processEvents()
            if not self.window.results.running:
                return
            QTest.qWait(10)
        self.fail("search did not finish")

    def _meeting(self, name: str, minutes_text: str = "") -> pathlib.Path:
        folder = self.recordings / name
        folder.mkdir()
        transcript.save(
            folder,
            [{"начало": 1.0, "конец": 2.0, "кто": "Я", "текст": "Начнем."}],
            {"длительность": 60, "провайдер": "OpenRouter", "стоимость": 0.1},
            [],
        )
        if minutes_text:
            (folder / minutes.FILE).write_text(minutes_text, encoding="utf-8")
        return folder

    def _element(self, folder: pathlib.Path):
        list_ = self.window.list_
        return next(
            list_.item(n)
            for n in range(list_.count())
            if (v := list_.item(n).data(Qt.ItemDataRole.UserRole)) and v.meeting.folder == folder
        )

    def _texts(self, folder: pathlib.Path) -> list[str]:
        line = self.window.list_.itemWidget(self._element(folder))
        return [
            (v.full() if hasattr(v, "full") else v.text()).strip()
            for v in line.findChildren(QLabel) + line.findChildren(QPushButton)
        ]

    def _click(self, folder: pathlib.Path, key_seq: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier) -> None:
        place = self.window.list_.visualItemRect(self._element(folder)).center()
        QTest.mouseClick(self.window.list_.viewport(), Qt.MouseButton.LeftButton, key_seq, place)
        app.processEvents()

    def test_scrolling_to_an_unmounted_meeting_keeps_its_actions(self):
        seed = catalog.meeting(self.ready_one)
        meetings = []
        for n in range(80):
            folder = self.recordings / f"meeting-{n}"
            folder.mkdir()
            date = seed.date - datetime.timedelta(minutes=n)
            meetings.append(dataclasses.replace(seed, folder=folder, started=date))
        self.window.show_meetings(meetings, {}, {}, None)
        app.processEvents()
        list_ = self.window.list_
        item = self._element(meetings[-1].folder)
        self.assertIsNone(list_.itemWidget(item))
        list_.scrollToBottom()
        for _ in range(4):
            app.processEvents()
        self.assertIsNotNone(list_.itemWidget(item))
        self._click(meetings[-1].folder)
        self.assertEqual(self.opened_all, [meetings[-1].folder])

    def test_click_opens_meeting_next_to_list_without_sending(self):
        sent: list = []
        self.window.recognize.connect(sent.append)
        self._click(self.ready_one)
        self._click(self.recorded)  # an unsent one can be viewed too; sending costs money, so only by the button
        self.assertEqual((self.opened_all, sent), ([self.ready_one, self.recorded], []))
        self._click(self.no_minutes, Qt.KeyboardModifier.ControlModifier)  # with Ctrl: select, as in Explorer
        self.assertEqual(len(self.opened_all), 2)

    def _section(self, section: str, window: MeetingsWindow | None = None) -> None:
        """A click on a section in the left column."""
        sections = (window or self.window).projects_column
        element = sections.element(section)
        list_ = element.listWidget()
        QTest.mouseClick(
            list_.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            list_.visualItemRect(element).center(),
        )
        app.processEvents()

    def test_left_sections_open_in_place_and_column_stays(self):
        """Tasks, summary, settings and help open to the right of the sections column, not over the whole window with
        a way back; the selected section is highlighted."""
        sections, opened_all = self.window.projects_column, []
        for chime, name in (
            (self.window.tasks, "задачи"),
            (self.window.summary, "сводка"),
            (self.window.config, "настройки"),
            (self.window.help_page, "справка"),
        ):
            chime.connect(lambda *p, i=name: opened_all.append((i, *p)))
        meetings = sections.element("встречи")
        self.assertTrue(meetings.text().startswith("Все встречи"))
        self.assertIsNot(meetings.listWidget(), sections.element("блокнот").listWidget())
        self.assertEqual(sections.section, "встречи")
        for section in ("задачи", "сводка", "настройки", "справка"):
            self._section(section)
        self.assertEqual(opened_all, [("задачи", None), ("сводка",), ("настройки",), ("справка",)])
        self.window.show_page(QLabel("доска"), "Задачи", "задачи")  # this is how the app opens it
        app.processEvents()
        self.assertEqual(
            (sections.isVisible(), self.window.meetings_column.isVisible(), sections.section), (True, False, "задачи")
        )
        self.assertNotIn("Все встречи", [k.text() for k in self.window.findChildren(QPushButton)])
        self.assertIn("Задачи", self.window.windowTitle())
        self._section("встречи")
        self.assertEqual(
            (self.window.page, self.window.meetings_column.isVisible(), sections.section), (None, True, "встречи")
        )

    def _click_project(self, code: str | None) -> None:
        panel = self.window.panel
        if code is None:
            self._section("встречи")
            return
        element = next(
            panel.item(n) for n in range(panel.count()) if panel.item(n).data(Qt.ItemDataRole.UserRole) == code
        )
        QTest.mouseClick(
            panel.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            panel.visualItemRect(element).center(),
        )

    def test_notebook_filter_saves_the_editor_and_is_independent_of_meetings(self):
        notebook.save(self.recordings, notebook.Note(title="Склад", project="Склад"), notebook.Note())
        notebook.save(self.recordings, notebook.Note(title="Личное"), notebook.Note())
        self._click_project("Склад")
        page = NotebookPage(self.recordings, ["Склад"])
        self.window.show_page(page, "Блокнот", "блокнот")
        app.processEvents()
        self.assertEqual(page.list_.count(), 2)
        page.project_button.menu().actions()[1].trigger()
        self.assertIs(self.window.page, page)
        self.assertTrue(self.window.projects_column.element("блокнот").isSelected())
        self.assertFalse(self.window.panel.selectedItems())
        self.assertEqual((page.project_button.text(), page.list_.count(), page.title.text()), ("Склад", 1, "Склад"))
        page.body.insertPlainText("Вопросы")
        page.heading.menu().actions()[2].trigger()
        self.assertEqual(page.body.toPlainText(), "Вопросы")
        self.assertEqual(page.body.textCursor().blockFormat().headingLevel(), 2)
        QTest.keyClick(page.body, Qt.Key.Key_Return)
        page.body.insertPlainText("Текст")
        page.project_button.menu().actions()[-1].trigger()
        self.assertEqual((page.project_button.text(), page.title.text()), ("Без проекта", "Личное"))
        page.project_button.menu().actions()[1].trigger()
        self.assertEqual(page.body.source(), "## Вопросы\n\nТекст")
        self.assertEqual(page.body.toPlainText(), "Вопросы\nТекст")
        self._section("блокнот")
        self.assertEqual(page.project_button.text(), "Склад")
        page.project_button.menu().actions()[0].trigger()
        self.assertEqual(page.list_.count(), 2)
        self._click_project("Склад")
        self.assertIsNone(self.window.page)
        self.assertEqual(self.window.project, "Склад")

    def test_notebook_moving_note_keeps_editor_within_current_project_filter(self):
        first = notebook.save(self.recordings, notebook.Note(title="Первая", project="Склад"), notebook.Note())
        second = notebook.save(self.recordings, notebook.Note(title="Вторая", project="Склад"), notebook.Note())
        page = NotebookPage(self.recordings, ["Склад", "Офис"])
        self.addCleanup(page.close)
        page._select_project("Склад")
        page._open(first)
        page.body.insertPlainText("Сохранить перед переносом")

        page._set_project("Офис")
        self.assertEqual((page.project_button.text(), page.title.text()), ("Склад", "Вторая"))
        self.assertEqual(page.list_.count(), 1)
        self.assertEqual(page.list_.currentItem().text(), second.title)

        page._set_project("Офис")
        self.assertEqual((page.project_button.text(), page.list_.count(), page.title.text()), ("Склад", 0, ""))
        self.assertEqual(page._current.project, "Склад")

        page._select_project(None)
        moved = next(z for z in notebook.list_(self.recordings) if z.id == first.id)
        self.assertEqual(moved.project, "Офис")
        page._open(moved)
        self.assertEqual(page.body.toPlainText(), "Сохранить перед переносом")
        page._set_project("Склад")
        self.assertEqual(page._current.id, first.id)
        self.assertEqual(page.list_.currentItem().text(), first.title)

    def test_rename_preserves_note_and_other_draft(self):
        note = notebook.save(
            self.recordings, notebook.Note(title="План", text="Текст", project="Склад"), notebook.Note()
        )
        other = notebook.save(self.recordings, notebook.Note(title="Черновик", pinned=True), notebook.Note())
        page = NotebookPage(self.recordings, ["Склад"])
        self.addCleanup(page.close)
        page.body.setPlainText("Новая строка")
        with mock.patch.object(QInputDialog, "getText", return_value=("Итоги", True)):
            page._rename(note)
        self.assertEqual(page._current.id, other.id)
        self.assertEqual(page.body.toPlainText(), "Новая строка")
        self.assertTrue(page.save())
        saved = next(item for item in notebook.list_(self.recordings) if item.id == note.id)
        self.assertEqual(dataclasses.replace(saved, updated=note.updated, title=note.title), note)
        self.assertEqual(saved.title, "Итоги")
        page._open(saved)
        for name, accepted in (("Отмена", False), ("  ", True)):
            with mock.patch.object(QInputDialog, "getText", return_value=(name, accepted)):
                page._rename(saved)
            self.assertEqual(page.title.text(), "Итоги")

    def test_notebook_delete_targets_the_clicked_note_and_keeps_other_edits(self):
        first = notebook.save(self.recordings, notebook.Note(title="Первая"), notebook.Note())
        second = notebook.save(self.recordings, notebook.Note(title="Вторая", pinned=True), notebook.Note())
        page = NotebookPage(self.recordings, [])
        self.addCleanup(page.close)
        page.title.setText("Правка второй")
        with mock.patch.object(style, "confirm", return_value=False):
            page._delete(first)
        self.assertEqual(notebook.list_(self.recordings), [second, first])
        with mock.patch.object(style, "confirm", return_value=True):
            page._delete(first)
            self.assertEqual(page.title.text(), "Правка второй")
            self.assertTrue(page.save())
            stale = notebook.list_(self.recordings)[0]
            self.assertEqual(stale.title, "Правка второй")
            updated = notebook.save(self.recordings, dataclasses.replace(stale, title="Новая правка"), stale)
            with mock.patch.object(QMessageBox, "warning") as warning:
                page._delete(stale)
            warning.assert_called_once()
            self.assertEqual(notebook.list_(self.recordings), [updated])
            page._open(updated)
            page.title.setText("Не сохранять удалённую заметку")
            page._delete(updated)
        self.assertTrue(page.save())
        self.assertEqual(notebook.list_(self.recordings), [])

    def test_notebook_restores_project_note_and_cursor_after_close(self):
        note = notebook.save(
            self.recordings,
            notebook.Note(title="План", text="Первый абзац\nВторой абзац", project="Склад"),
            notebook.Note(),
        )
        notebook.save(self.recordings, notebook.Note(title="Другая", project="Склад"), notebook.Note())
        page = NotebookPage(self.recordings, ["Склад"])
        page.project_button.menu().actions()[1].trigger()
        page._open(note)
        cursor = page.body.textCursor()
        cursor.setPosition(15)
        page.body.setTextCursor(cursor)
        self.assertTrue(page.close())
        reopened = NotebookPage(self.recordings, ["Склад"])
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.project_button.text(), "Склад")
        self.assertEqual(reopened.title.text(), "План")
        self.assertEqual(reopened.body.textCursor().position(), 15)

    def test_pending_filter_updates_when_minutes_are_written(self):
        def visible():
            return {v.meeting.folder for n in range(self.window.list_.count()) if (v := self._data(n))}

        self.window.pending_only.click()
        self.assertEqual(visible(), {self.no_minutes, self.recorded})
        (self.no_minutes / minutes.FILE).write_text("# Итоги\n", encoding="utf-8")
        self.window.show_meetings(catalog.list_(self.recordings), {}, {}, None)
        self.assertEqual(visible(), {self.recorded})
        self.window.pending_only.click()
        self.assertEqual(visible(), {self.ready_one, self.no_minutes, self.recorded})

    def test_notebook_formatting_and_table_survive_reopen(self):
        legacy = notebook.Note(title="Заметка", text="Ссылка\n\n\nПолужирный текст")
        saved = notebook.save(self.recordings, legacy, notebook.Note())
        page = NotebookPage(self.recordings, [])
        self.addCleanup(page.close)
        self.assertTrue(page.save())
        self.assertEqual(notebook.list_(self.recordings), [saved])
        editor = page.body
        self.assertTrue(editor.find("Ссылка"))
        page._format(editor.set_link, link_url("https://example.org"))
        self.assertTrue(editor.find("Полужирный текст"))
        page._styles[BOLD].click()
        page._format(editor.set_list, NUMBER)
        editor.moveCursor(QTextCursor.MoveOperation.End)
        page._format(editor.insert_table, 2, 2)
        editor.insertPlainText("Срок")
        page._format(editor.edit_table, "col+")
        self.assertTrue(page.save())
        reopened = NotebookPage(self.recordings, [])
        self.addCleanup(reopened.close)
        self.assertIn("**Полужирный текст**", reopened.body.source())
        self.assertIn("[Ссылка](https://example.org)", reopened.body.source())
        self.assertIn("Ссылка\n\n\nПолужирный текст", reopened.body.toPlainText())
        self.assertTrue(reopened.body.find("Срок"))
        table = reopened.body.textCursor().currentTable()
        self.assertEqual((table.rows(), table.columns()), (2, 3))
        self.assertIsNone(link_url("file:///C:/private.txt"))
        self.assertIsNone(link_url("https:"))

    def test_drop_hint_is_one_short_line(self):
        """The hint under the list fits one line of an ordinary list column; formats are in its tooltip."""
        self.window.resize(1400, 800)
        app.processEvents()
        hint = next(label for label in self.window.findChildren(QLabel) if "href='file'" in label.text())
        self.assertLess(hint.height(), 2 * hint.fontMetrics().lineSpacing())
        self.assertIn("mp4", hint.toolTip())

    def test_project_click_shows_its_page_over_an_open_meeting(self):
        """A click on a project shows its page instead of the open meeting, also a click on the project already
        selected; the meeting row is not left highlighted. "All projects" has no page: the meeting stays."""
        self.window.recordings_folder = self.recordings
        self.window.show_meeting(QLabel("встреча"), "Встреча")
        self._click_project("Склад")
        self.assertIsNone(self.window.meeting)
        self.assertIs(self.window.details.currentWidget(), self.window.project_page)
        self.assertEqual(self.window.list_.selectedItems(), [])
        self.window.show_meeting(QLabel("снова"), "Встреча")
        self._click_project("Склад")  # already selected
        self.assertIsNone(self.window.meeting)
        self.window.show_meeting(QLabel("еще раз"), "Встреча")
        self._click_project(None)
        self.assertIsNotNone(self.window.meeting)

    def test_task_board_filters_locally_and_sidebar_project_opens_meetings(self):
        page = TasksWindow(self.recordings, (["Склад"], {}))
        self.window.show_page(page, "Задачи", "задачи")
        page._project_button.menu().actions()[1].trigger()
        self.assertEqual(page.project, "Склад")
        self.assertIsNone(self.window.project)
        self.assertFalse(self.window.panel.selectedItems())
        self._click_project("Склад")
        self.assertIsNone(self.window.page)
        self.assertEqual(self.window.project, "Склад")
        opened = []
        self.window.tasks.connect(opened.append)
        self._section("задачи")
        self.assertEqual(opened, [None])

    def test_help_takes_the_search_box_and_gives_it_back(self):
        """In the help the search box at the top searches the help; the meeting query comes back after it."""
        self.window.search.setText("Начнем")
        page = help_page.HelpPage()
        self.window.show_page(page, "Справка", "справка")
        self.assertEqual(self.window.search.text(), "")
        self.assertEqual(self.window.search.placeholderText(), page.search_hint)
        self.assertFalse(self.window._search_delay.isActive())  # the meeting query typed just before is applied
        self._wait_for_search()
        self.assertEqual(self.window.results.wanted.query, "Начнем")
        self.window.search.setText("ключ")
        self.assertTrue(page.segments.isHidden())  # the help shows found answers
        self.window._show_items(always=True)  # a list refresh meanwhile keeps the meeting query
        self._wait_for_search()
        self.assertEqual(self.window.results.wanted.query, "Начнем")
        self.window.go_back()
        self.assertEqual(self.window.search.text(), "Начнем")
        self.assertNotEqual(self.window.search.placeholderText(), page.search_hint)
        self.assertEqual(self.window.results.wanted.query, "Начнем")
        self.assertGreater(self.window.results.count(), 0)

    def test_page_changes_its_search_hint(self):
        """The board says what it searches when its own project button changes the project."""

        class Page(QWidget):
            search_hint_changed = Signal(str)
            search_hint = "Поиск по всем задачам  Ctrl+F"
            query = ""

            def search(self, query: str) -> None:
                self.query = query

        page = Page()
        self.window.show_page(page, "Задачи", "задачи")
        page.search_hint_changed.emit("Поиск по задачам проекта Склад  Ctrl+F")
        self.assertEqual(self.window.search.placeholderText(), "Поиск по задачам проекта Склад  Ctrl+F")
        self.window.search.setText("акт")
        self.assertEqual(page.query, "акт")

    def test_esc_clears_the_search_box(self):
        self.window.search.setText("Начнем")
        QTest.keyClick(self.window.search, Qt.Key.Key_Escape)
        self.assertEqual(self.window.search.text(), "")

    def test_column_collapses_to_strip_and_stays(self):
        """The collapsed column is a strip of icons with a tooltip on hover; reading mode (F11) collapses it and hides
        the list, a click on "Meetings" brings everything back; the layout is remembered across restarts."""
        layouts: list[dict] = []
        self.window.layout_changed.connect(layouts.append)
        meeting = MeetingView(self.ready_one)
        self.window.show_meeting(meeting, "Встреча")
        sections = self.window.projects_column
        self._section("свернуть")
        self.assertTrue(sections.isVisible() and sections.collapsed)
        self.assertLessEqual(sections.width(), 56)
        element = sections.element("задачи")
        self.assertEqual((element.text(), element.toolTip()), ("", "Задачи"))
        self.assertEqual(self.window.panel.item(0).text(), "")  # projects as icons too, the name in the tooltip
        self.assertIn("Склад", self.window.panel.item(0).toolTip())
        self.assertEqual(layouts[-1]["скрыто"], ["проекты"])
        self.window.expand()
        app.processEvents()
        self.assertEqual(
            (sections.isVisible(), self.window.meetings_column.isVisible(), meeting.full_width_button.text()),
            (True, False, meeting_view.FROM_FULL),
        )
        self._section("встречи")
        self.assertEqual(
            (sections.collapsed, self.window.meetings_column.isVisible(), meeting.full_width_button.text()),
            (False, True, meeting_view.FULL_WIDTH),
        )
        other_value = MeetingsWindow()
        other_value.set_layout({"скрыто": ["проекты", "встречи"]})  # as remembered before the restart
        other_value.show()
        app.processEvents()
        self.assertEqual(
            (
                other_value.projects_column.isVisible(),
                other_value.projects_column.collapsed,
                other_value.meetings_column.isVisible(),
            ),
            (True, True, False),
        )
        other_value.close()

    def test_narrow_window_folds_sections_not_the_list(self):
        """When the three columns do not fit, the sections fold to icons and the list keeps a readable width; a wider
        window unfolds them again; the automatic fold is not remembered."""
        self.window.show_meeting(MeetingView(self.ready_one), "Встреча")
        sections = self.window.projects_column
        self.window.resize(1500, 700)
        app.processEvents()
        self.assertFalse(sections.collapsed)
        self.window.resize(950, 700)
        app.processEvents()
        self.assertTrue(sections.collapsed)
        self.assertGreaterEqual(self.window.meetings_column.width(), meetings_window.LIST_MIN_WIDTH)
        self.assertNotIn("проекты", self.window.window_layout()["скрыто"])
        self.window.resize(1500, 700)
        app.processEvents()
        self.assertFalse(sections.collapsed)

    def test_narrow_row_keeps_buttons_off_the_title(self):
        """In a narrow column the "Transcribe" button shows only its icon, the words stay in the tooltip: it does not
        cover the title and the project; in a wide column it has its words again."""
        catalog.set_(self.recorded, project="Склад")
        self.window.show_meetings(catalog.list_(self.recordings), {}, {}, None)
        app.processEvents()
        line = self.window.list_.itemWidget(self._element(self.recorded))
        button = next(b for b in line.findChildren(QPushButton) if b.toolTip())
        line.resize(meetings_window.LIST_MIN_WIDTH - 20, line.height())
        app.processEvents()
        self.assertEqual(button.text(), "")
        self.assertLessEqual(line.minimumSizeHint().width(), line.width())
        line.resize(700, line.height())
        app.processEvents()
        self.assertIn("Распознать", button.text())

    def test_hidden_list_comes_back_by_a_visible_button(self):
        """The list hidden by "‹" in its header comes back by a button where it was, not only by Ctrl+2 or a click on
        the already highlighted "Meetings"; with a meeting open too. Reading at full width has its own button."""
        self.window.show_column("встречи", False)
        app.processEvents()
        self.assertFalse(self.window.meetings_column.isVisible())
        self.assertTrue(self.window.list_button.isVisible())
        self.window.list_button.click()
        app.processEvents()
        self.assertTrue(self.window.meetings_column.isVisible())
        self.assertFalse(self.window.list_button.isVisible())
        self.window.show_meeting(MeetingView(self.ready_one), "Встреча")
        self.window.show_column("встречи", False)
        app.processEvents()
        self.assertTrue(self.window.list_button.isVisible())
        self.window.show_column("встречи", True)
        self.window.expand()
        app.processEvents()
        self.assertFalse(self.window.list_button.isVisible())

    def test_sections_folded_by_hand_stay_folded(self):
        self.window.show_meeting(MeetingView(self.ready_one), "Встреча")
        self._section("свернуть")
        self.window.resize(950, 700)
        app.processEvents()
        self.window.resize(1500, 700)
        app.processEvents()
        self.assertTrue(self.window.projects_column.collapsed)

    def test_list_keys_do_not_act_in_open_meeting(self):
        calls: list[str] = []
        with mock.patch.object(MeetingsWindow, "_delete", lambda _window: calls.append("удалить")):
            window = MeetingsWindow()
        window.show_meetings(catalog.list_(self.recordings), {}, {}, None)
        window.show()
        meeting = MeetingView(self.ready_one)
        window.show_meeting(meeting, "Встреча")
        window.activateWindow()
        meeting.scroll_area.setFocus()
        app.processEvents()
        QTest.keyClick(meeting.scroll_area, Qt.Key.Key_Delete)
        self.assertEqual(calls, [])
        window.list_.setFocus()
        QTest.keyClick(window.list_, Qt.Key.Key_Delete)
        self.assertEqual(calls, ["удалить"])
        self.assertIs(window.details.currentWidget(), meeting)
        window.close()

    def test_project_color_and_icon_follow_rename_and_go_on_delete(self):
        changes: list[dict] = []
        self.window.project_looks_changed.connect(changes.append)
        catalog.set_(self.ready_one, project="Склад")
        self.window.set_projects(["Склад"], {})
        self.window.show_meetings(catalog.list_(self.recordings), {}, {}, None)
        self.window._set_project_kind("Склад", color=2)
        self.window._set_project_kind("Склад", icon="коробка")
        self.assertEqual(changes[-1], {"Склад": {"цвет": 2, "значок": "коробка"}})
        line = self.window.list_.itemWidget(self._element(self.ready_one))
        chip = next(m for m in line.findChildren(QLabel) if m.text() == "Склад")
        self.assertEqual(chip.styleSheet(), style.project_chip(2))
        with mock.patch.object(meetings_window.QInputDialog, "getText", return_value=("Остатки", True)):
            self.window._rename_project("Склад")
        self.assertEqual(changes[-1], {"Остатки": {"цвет": 2, "значок": "коробка"}})
        with mock.patch.object(meetings_window.QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.window._delete_project("Остатки")
        self.assertEqual(changes[-1], {})

    def test_project_rename_that_fails_halfway_changes_nothing(self):
        """The disk fills up while the project's tasks are written: its meetings stay in it, the settings keep the
        old name and its color, the user is told why; deleting it the same way leaves it too."""
        catalog.set_(self.ready_one, project="Склад")
        self.window.recordings_folder = self.recordings
        self.window.set_projects(["Склад"], {"Склад": {"цвет": 2}})
        self.window.show_meetings(catalog.list_(self.recordings), {}, {}, None)
        saved, notices = [], []
        self.window.projects_changed.connect(saved.append)
        self.window.project_looks_changed.connect(saved.append)
        self.window.notice.connect(lambda heading, _text: notices.append(heading))
        full = OSError(errno.ENOSPC, "No space left on device", "задачи.json")
        with (
            mock.patch.object(tasks, "rename_project", side_effect=full),
            mock.patch.object(meetings_window.QInputDialog, "getText", return_value=("Остатки", True)),
            mock.patch.object(meetings_window.QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes),
            self.assertLogs("knotes", "WARNING"),
        ):
            self.window._rename_project("Склад")
            self.window._delete_project("Склад")
        self.assertEqual(catalog.properties(self.ready_one)["проект"], "Склад")
        self.assertEqual(saved, [])
        self.assertEqual(notices, ["Не удалось переименовать проект", "Не удалось удалить проект"])

    def test_foreign_text_in_tooltip_not_markup(self):
        """A title "<img src=...>" from a calendar invitation must not load an image: the tooltip shows it as text."""
        caption = style.Elided("<img src='file:///C:/x.png'> Встреча", "<b>подсказка</b>")
        self.assertIn("&lt;b&gt;подсказка&lt;/b&gt;", caption.toolTip())
        caption.resize(40, 20)
        caption.resizeEvent(QResizeEvent(caption.size(), caption.size()))
        self.assertIn("&lt;img", caption.toolTip())
        self.assertNotIn("<img", caption.toolTip())
        caption.deleteLater()

    def test_project_default_kind_from_menu(self):
        menu = QMenu()
        projects_panel.minutes_menu(menu, "Склад", self.recordings)
        submenu = menu.actions()[0].menu()
        self.assertEqual([d.text() for d in submenu.actions() if d.isChecked()], [kinds.WORKING])
        next(d for d in submenu.actions() if d.text() == "Обследование").trigger()
        self.assertEqual(projects.kind(self.recordings, "Склад"), "Обследование")
        catalog.set_(self.ready_one, project="Склад")
        self.assertEqual(kinds.meeting_kind(self.ready_one).name, "Обследование")
        menu.deleteLater()

    def test_result_in_row_without_ready_mark_and_extra_button(self):
        """A ready meeting opens with a click on its row: it needs no button, even if the audio is still next to it.
        An unsent one has "Transcribe" with the price in the tooltip, and no "Not sent" mark repeating the button."""
        (self.ready_one / "микрофон.flac").write_bytes(b"x")  # the audio stays after recognition
        self.window._snapshot = ()  # the list is rebuilt only when something changed; the audio is not in the snapshot
        self.window.show_meetings(catalog.list_(self.recordings), {}, {}, None)
        ready_one = self._texts(self.ready_one)
        self.assertIn("3 решения · 1 задача · 2 вопроса", ready_one)
        for extra in ("Готова", "Протокол", "Расшифровка", "Распознать"):
            self.assertFalse(any(tr.startswith(extra) for tr in ready_one), extra)
        recorded = self._texts(self.recorded)
        self.assertIn("Распознать", recorded)
        self.assertNotIn("Не отправлена", recorded)

    def test_row_not_wider_than_column(self):
        """A long title and summary are shortened, buttons do not go past the edge of a narrow column."""
        catalog.set_(self.recorded, title="Очень длинное название встречи про загрузку остатков из Excel")
        self.window.show_meetings(catalog.list_(self.recordings), {}, {}, None)
        self.window.columns.setSizes([150, 260, 900])
        app.processEvents()
        list_ = self.window.list_
        self.assertLessEqual(list_.visualItemRect(self._element(self.recorded)).width(), list_.viewport().width())
        self.assertFalse(list_.horizontalScrollBar().isVisible())

    def test_progress_rebuilds_only_its_row(self):
        """Local recognition reports every part: only the busy meeting's row changes, the list is not rebuilt, and
        the next refresh with the same state leaves it alone too."""
        busy = {self.recorded: Busy.TRANSCRIBING}
        progress = {self.recorded: meeting_rows.Progress(1, 4, 0.0)}
        self.window.show_meetings(catalog.list_(self.recordings), busy, progress, None)
        others = self.window.list_.itemWidget(self._element(self.ready_one))
        progress[self.recorded] = meeting_rows.Progress(2, 4, 0.0)
        self.window.show_progress(self.recorded, progress[self.recorded])
        self.assertIn("Распознается, 2 из 4", self._texts(self.recorded))
        self.assertIs(self.window.list_.itemWidget(self._element(self.ready_one)), others)
        self.window.show_meetings(catalog.list_(self.recordings), busy, progress, None)
        self.assertIs(self.window.list_.itemWidget(self._element(self.ready_one)), others)

    def test_unreadable_minutes_cost_only_their_row(self):
        """Minutes in ANSI or cut off: the row goes without the summary, the rows after it are in the list."""
        (self.ready_one / minutes.FILE).write_bytes("# Итоги\n\n## Решения\n\n1. Цена.\n".encode("cp1251"))
        self.window._snapshot = ()  # the old code does not see a change of the minutes alone
        self.window.show_meetings(catalog.list_(self.recordings), {}, {}, None)
        self.assertEqual(
            {v.meeting.folder for n in range(self.window.list_.count()) if (v := self._data(n))},
            {self.ready_one, self.no_minutes, self.recorded},
        )

    def _data(self, number: int):
        return self.window.list_.item(number).data(Qt.ItemDataRole.UserRole)

    def test_failed_rebuild_is_tried_again(self):
        """The list remembers what it shows only once it is shown: a rebuild cut off by an error is done again on the
        next refresh."""
        catalog.set_(self.recorded, title="Новое название")
        with (
            mock.patch.object(meetings_window, "MeetingRow", side_effect=RuntimeError("сбой")),
            self.assertRaises(RuntimeError),
        ):
            self.window.show_meetings(catalog.list_(self.recordings), {}, {}, None)
        self.window.show_meetings(catalog.list_(self.recordings), {}, {}, None)
        self.assertIn("Новое название", self._texts(self.recorded))

    def test_list_in_sight(self):
        self.assertTrue(self.window.list_in_sight())
        shown_again: list[bool] = []
        self.window.in_sight.connect(lambda: shown_again.append(True))
        self.window.show_page(QLabel("доска"), "Задачи", "задачи")
        self.assertFalse(self.window.list_in_sight())
        self.window.go_back()
        self.assertTrue(self.window.list_in_sight())
        self.assertEqual(shown_again, [True])
        self.window.showMinimized()
        app.processEvents()
        self.assertFalse(self.window.list_in_sight())

    def test_open_meeting_deleted_with_others_is_closed(self):
        """The open meeting deleted together with others does not stay open and silent: its card closes first (its
        files are let go) and the user is told."""
        meeting = MeetingView(self.ready_one)
        meeting.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)  # as the program opens it
        watcher = meeting.minutes._watcher
        self.window.show_meeting(meeting, "Встреча")
        for folder in (self.ready_one, self.no_minutes):
            self._element(folder).setSelected(True)
        notices: list[tuple[str, str]] = []
        self.window.notice.connect(lambda heading, text: notices.append((heading, text)))
        deleted: list[tuple[pathlib.Path, bool, list]] = []

        def to_recycle_bin(folder: pathlib.Path) -> bool:  # the card has let go of the folder by now
            deleted.append(
                (folder, shiboken6.isValid(meeting), watcher.directories() if shiboken6.isValid(watcher) else [])
            )
            return True

        with (
            mock.patch.object(meetings_window.QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes),
            mock.patch.object(meetings_window.system, "to_recycle_bin", side_effect=to_recycle_bin),
        ):
            self.window._delete()
        self.assertIsNone(self.window.meeting)
        self.assertEqual(sorted(deleted), [(self.ready_one, False, []), (self.no_minutes, False, [])])
        self.assertEqual(len(notices), 1)
        self.assertIn(catalog.display_title(self.ready_one), notices[0][1])

    def test_meeting_that_keeps_its_edit_is_not_deleted(self):
        meeting = MeetingView(self.ready_one)
        self.window.show_meeting(meeting, "Встреча")
        self._element(self.ready_one).setSelected(True)
        deleted: list[pathlib.Path] = []
        with (
            mock.patch.object(meetings_window.QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes),
            mock.patch.object(meeting, "close", return_value=False),  # "Close without saving?" - "No"
            mock.patch.object(meetings_window.system, "to_recycle_bin", side_effect=lambda p: deleted.append(p) or 1),
        ):
            self.window._delete()
        self.assertIs(self.window.meeting, meeting)
        self.assertEqual(deleted, [])


if __name__ == "__main__":
    unittest.main()
