# SPDX-License-Identifier: GPL-3.0-only
"""Project page: "Prepare questions" asks the model in the background once, the answer goes under the analyst's text
(also after the page closed or was rebuilt), a failure is plain and keeps the prep; typing in the prep does not read
every meeting and the board again."""

import pathlib
import tempfile
import unittest
from unittest import mock

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication

from knotes import catalog, minutes, prep, projects, providers, settings, tasks
from knotes.ui import project_page
from knotes.ui.project_page import ProjectPage

app = QApplication.instance() or QApplication([])
ANSWER = prep.Suggestion(("Как сейчас - Кто вносит остатки?", "До встречи - Запросить копию базы."), 0.002)


class TestPrepareQuestions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.recordings = pathlib.Path(self._tmp.name)
        projects.set_for_meeting(self.recordings, "Склад", "Сроки")
        self.requests: list[tuple] = []
        background = mock.patch.object(
            project_page, "run_in_background", side_effect=lambda _pool, work, done: self.requests.append((work, done))
        )
        background.start()
        self.addCleanup(background.stop)

    def tearDown(self):
        project_page._preparing.clear()
        app.processEvents()
        self._tmp.cleanup()

    def _page(self) -> ProjectPage:
        """Pages are deleted after the test while Qt is still running: otherwise Python deletes them after Qt."""
        page = ProjectPage(self.recordings, "Склад")
        self.addCleanup(app.processEvents)
        self.addCleanup(page.deleteLater)
        return page

    def test_one_request_in_background_and_answer_under_user_text(self):
        page = self._page()
        page._prepare_button.click()
        self.assertEqual(page._prepare_button.text(), "Готовлю…")
        self.assertFalse(page._prepare_button.isEnabled())
        page._prepare()  # a second click while the model answers
        self.assertEqual(len(self.requests), 1)
        work, done = self.requests[0]
        with (
            mock.patch.object(settings, "load", return_value=settings.Settings()),
            mock.patch.object(minutes, "params", return_value="параметры"),
            mock.patch.object(prep, "suggest", return_value=ANSWER) as suggest,
        ):
            self.assertIs(work(), ANSWER)
        suggest.assert_called_once_with(self.recordings, "Склад", "параметры")
        cursor = QTextCursor(page.questions.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText("\nкто вносит остатки")  # typed while the model answered
        done(ANSWER)
        self.assertEqual(page.questions.toPlainText(), "Сроки\nкто вносит остатки\nДо встречи - Запросить копию базы.")
        self.assertEqual(page._prepare_button.text(), "Подготовить вопросы")
        self.assertTrue(page._prepare_button.isEnabled())
        self.assertTrue(page._prepare_state.text().startswith("Добавлено в подготовку: 1"))
        page.save()
        self.assertEqual(projects.for_meeting(self.recordings, "Склад"), page.questions.toPlainText())
        self.assertIn("Запросить копию базы", page.agenda.text())
        page.questions.undo()
        self.assertEqual(page.questions.toPlainText(), "Сроки\nкто вносит остатки")

    def test_failure_plain_and_prep_kept(self):
        page = self._page()
        page._prepare_button.click()
        self.requests[0][1](providers.ProviderError("OpenRouter не ответил: перегружен"))
        self.assertEqual(page._prepare_state.text(), "OpenRouter не ответил: перегружен")
        self.assertEqual(page._prepare_state.objectName(), "ошибка_текст")
        self.assertEqual(page.questions.toPlainText(), "Сроки")
        self.assertTrue(page._prepare_button.isEnabled())
        page._prepare_button.click()  # a new request after the failure
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(page._prepare_state.text(), "")

    def test_answer_after_page_closed_or_rebuilt_is_not_lost(self):
        page = self._page()
        page._prepare_button.click()
        page.close()
        self.requests[-1][1](ANSWER)
        self.assertEqual(
            projects.for_meeting(self.recordings, "Склад"),
            "Сроки\nКак сейчас - Кто вносит остатки?\nДо встречи - Запросить копию базы.",
        )
        projects.set_for_meeting(self.recordings, "Склад", "Сроки")
        page = self._page()
        page._prepare_button.click()
        page.close()
        rebuilt = self._page()
        self.assertFalse(rebuilt._prepare_button.isEnabled())
        rebuilt._prepare()
        self.assertEqual(len(self.requests), 2)
        self.requests[-1][1](ANSWER)
        self.assertIn("До встречи - Запросить копию базы.", rebuilt.questions.toPlainText())
        rebuilt.save()
        renamed = self._page()
        renamed._prepare_button.click()
        renamed.close_without_saving()  # the project was renamed: the answer has nowhere to go
        before = projects.for_meeting(self.recordings, "Склад")
        self.requests[-1][1](prep.Suggestion(("Новый вопрос?",)))
        self.assertEqual(projects.for_meeting(self.recordings, "Склад"), before)

    def test_nothing_new(self):
        page = self._page()
        page._prepare_button.click()
        self.requests[0][1](prep.Suggestion(("Сроки?",), 0.0))
        self.assertEqual(page.questions.toPlainText(), "Сроки")
        self.assertTrue(page._prepare_state.text().startswith("Нового нет"))

    def test_typing_in_prep_does_not_read_meetings_again(self):
        """Every pause in typing saved the prep and read all meetings and the board: on a big project the window
        froze. The past is read once with the page."""
        page = self._page()
        with (
            mock.patch.object(tasks, "tasks", side_effect=AssertionError("the board is read again")),
            mock.patch.object(catalog, "list_", side_effect=AssertionError("the meetings are read again")),
        ):
            page.questions.appendPlainText("Кто подписывает акты?")
            page.save()
            self.assertIn("Кто подписывает акты?", page.agenda.text())
            page._copy_button()
            page.close()
        self.assertEqual(projects.for_meeting(self.recordings, "Склад"), "Сроки\nКто подписывает акты?")
        self.assertIn("Кто подписывает акты?", QApplication.clipboard().text())


if __name__ == "__main__":
    unittest.main()
