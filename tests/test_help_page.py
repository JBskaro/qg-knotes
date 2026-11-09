# SPDX-License-Identifier: GPL-3.0-only
"""Help: questions by topic, a click opens the answer as steps, the search finds answers by the start of a word and
shows a piece of the answer, a link leads to its settings section."""

import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from knotes import language
from knotes import help_topics
from knotes.ui import help_page
from knotes.ui.settings_data import SECTIONS
from tests.shown_windows import shown_windows

program = QApplication.instance() or QApplication([])


class TestHelpPage(unittest.TestCase):
    def setUp(self):
        language.set_("ru")
        with shown_windows() as self.flashed:
            self.page = help_page.HelpPage()

    def tearDown(self):
        self.page.deleteLater()

    def _shown(self) -> list[str]:
        return [question.answer.question for _, question in self.page.questions if not question.isHidden()]

    def _question(self, text: str) -> help_page.Question:
        return next(question for _, question in self.page.questions if question.answer.question == text)

    def test_topic_shows_its_questions(self):
        self.assertEqual(self.flashed, [])
        self.assertEqual(self._shown(), [answer.question for answer in help_topics.RECORDING])
        self.page.topics.button(2).click()
        self.assertEqual(self._shown(), [answer.question for answer in help_topics.MINUTES])
        self.assertTrue(all(heading.isHidden() for heading in self.page.headings))  # the segment names the topic

    def test_click_opens_answer_as_steps_with_link(self):
        question, sections = self._question("Как составить протокол?"), []
        self.page.open_settings.connect(sections.append)
        self.assertTrue(question.body.isHidden())
        question.header.click()
        self.assertFalse(question.body.isHidden())
        self.assertIn("<ol", question.body.text())
        self.assertIn("<b>Составить протокол</b>", question.body.text())
        self.assertIn("Открыть раздел «Протоколы» в настройках", question.body.text())
        question.body.linkActivated.emit("протоколы")
        self.assertEqual(sections, ["протоколы"])
        question.header.click()
        self.assertTrue(question.body.isHidden())

    def test_opened_answer_follows_theme(self):
        question = self.page.questions[0][1]
        question.set_open(True)
        with mock.patch.object(help_page.style, "text_tones", return_value=("#FF0000", "#00FF00")):
            self.page.repaint_theme()
        self.assertIn("#00FF00", question.body.text())  # the quiet note in the new theme
        self.assertTrue(question.opened)

    def test_answers_lead_to_existing_settings_sections(self):
        leads = {answer.settings for _, answers in help_topics.TOPICS for answer in answers} - {""}
        self.assertLessEqual(leads, set(SECTIONS))

    def test_search_by_start_of_word_with_piece_of_answer(self):
        self.page.search("ключ")
        shown = self._shown()
        self.assertIn("Как подключить другой сервис?", shown)  # "Впишите ключ"
        self.assertNotIn("Можно записывать экран встречи?", shown)  # "включите" does not start with "ключ"
        piece = self._question("Как подключить другой сервис?").piece
        self.assertFalse(piece.isHidden())
        self.assertRegex(piece.text(), r"<b [^>]*>ключ</b>")
        self.assertTrue(self.page.segments.isHidden())
        self.assertIn(str(len(shown)), self.page.lead.text())
        self.assertFalse(self.page.headings[4].isHidden())  # "Подключения" has hits
        self.assertTrue(self.page.headings[0].isHidden())  # "Запись" has none

    def test_all_words_nothing_found_and_back_to_topic(self):
        self.page.topics.button(4).click()
        self.page.search("ключ windows")
        self.assertEqual(
            self._shown(), ["Как подключить другой сервис?", "Как подключить календарь?", "Где лежат файлы?"]
        )
        self.page.search("нетакогослова")
        self.assertEqual(self._shown(), [])
        self.assertIn("ничего не нашлось", self.page.lead.text())
        self.page.search("")
        self.assertEqual(self._shown(), [answer.question for answer in help_topics.CONNECTIONS])
        self.assertFalse(self.page.segments.isHidden())

    def test_piece_starts_near_a_far_word(self):
        piece = help_page.snippet(self._question("Как составить протокол?").answer, "базы")
        self.assertTrue(piece.startswith("…"))
        self.assertIn(">базы</b>", piece)

    def test_buttons_of_getting_started(self):
        asked: list[str] = []
        self.page.tips_again.connect(lambda: asked.append("tips"))
        self.page.check_sound.connect(lambda: asked.append("sound"))
        buttons = [button for button in self.page.start.findChildren(help_page.QPushButton)]
        for button in buttons:
            button.click()
        self.assertEqual(asked, ["tips", "sound"])
