# SPDX-License-Identifier: GPL-3.0-only
"""Minutes in the language of the meeting: an English meeting gets English headings and template phrases, and the
program reads English minutes as it reads Russian ones: blocks, who and when, the board, the email."""

import pathlib
import tempfile
import unittest

from knotes import tasks, catalog, providers, minutes, transcript, kinds

ENGLISH = """# Minutes of the meeting on 14.10.2026 about the stock import

Hello,

Here are the minutes of our meeting on 14.10.2026 about the stock import.

Participants - Dmitry, Anna

## What was shown

1. The stock import from the warehouse file. [01:10]

## Remarks

1. The report has no total per warehouse, a total for each warehouse is needed. Igor, 20.10.2026. [03:20]

## Decisions

1. The price comes from the price list. Tentative, Anna will confirm with the chief accountant. [05:40]
2. The quantity comes from the file. [06:10]

## Tasks

1. Put the import on the test database. Igor, 16.10.2026. [09:10]

## Open Questions

1. How to convert boxes to pieces. Anna will ask the warehouse. [09:18]

## Next meeting

19.10.2026. Showing the import.

Transcript - 2026-10-14_15-00, расшифровка.md

## For the analyst

1. On task 1. Igor suspects a duplicated document. [11:10]
"""


class TestMinutesLanguage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.recordings = pathlib.Path(self._tmp.name)
        self.folder = self.recordings / "2026-10-14_15-00"
        self.folder.mkdir()
        s = providers.find("openrouter", "протоколы")
        self.params = minutes.Params(s, "к", s.model, "И")

    def tearDown(self):
        self._tmp.cleanup()

    def _say(self, *texts: str) -> None:
        phrases = [{"начало": n * 5.0, "конец": n * 5.0 + 4, "кто": "Я", "текст": z} for n, z in enumerate(texts)]
        transcript.save(self.folder, phrases, {"длительность": 600, "провайдер": "x", "стоимость": 0.0}, [])

    def test_english_meeting_gets_english_headings_and_phrases(self):
        self._say("Let's look at the stock import.", "The price comes from the price list, right?")
        catalog.set_(self.folder, kind="Показ доработки")
        input_ = minutes.input_(self.folder, self.params)
        self.assertIn("Язык протокола - английский", input_)
        for part in (
            "- What was shown («Что показали»). Пункт ",
            "- Remarks («Замечания»). Пункт ",
            "- Next meeting («Следующая встреча»). Пункт ",
            "- For the analyst («Для аналитика»). Пункт ",
            "«Добрый день!» - «Hello,»",
            "«Участники - <имена через запятую>» - «Participants - <names separated by commas>»",
            "«Предварительно, <кто подтвердит>.» - «Tentative, <who confirms>.»",
            "«Просим сообщить» - «Please let us know»",
        ):
            self.assertIn(part, input_)
        self.assertLess(input_.index("Язык протокола"), input_.index("[00:00] Я: Let's look"))

    def test_owner_without_a_name_is_me_in_english(self):
        """«Я» is the owner's label in the transcript: English minutes without the owner's name write «Me»."""
        self._say("Let's look at the stock import.")
        self.assertIn("«Я» - «Me»", minutes.input_(self.folder, self.params))
        self.assertIn("перевод «Я» из фраз шаблона", minutes.DEFAULT_INSTRUCTION.read_text(encoding="utf-8"))

    def test_russian_meeting_with_english_terms_stays_russian(self):
        self._say("Смотрим загрузку остатков из Excel.", "Report по складу пока не трогаем, OK?")
        input_ = minutes.input_(self.folder, self.params)
        self.assertIn("Язык протокола - русский", input_)
        self.assertIn("- Решения. Пункт ", input_)
        self.assertNotIn("Hello,", input_)

    def test_every_language_has_all_headings_and_phrases_of_the_instruction(self):
        """A heading or phrase missing in a language leaves the model to guess it and the program unable to read it
        back; a phrase the instruction no longer has is a leftover."""
        written = "".join(z.read_text(encoding="utf-8") for z in (minutes.DEFAULT_INSTRUCTION, kinds.DESCRIPTIONS))
        self.assertEqual(minutes.languages()[0].code, "ru")
        for language_ in minutes.languages()[1:]:
            with self.subTest(language=language_.code):
                self.assertEqual([z for z, o in kinds.descriptions().items() if language_.code not in o.names], [])
                self.assertEqual([z for z in language_.phrases if z not in written], [])
                for template in (minutes.PARTICIPANTS, minutes.TRANSCRIPT_LINE, minutes.TENTATIVE):
                    self.assertIn(template, language_.phrases)

    def test_english_headings_are_the_same_sections(self):
        self.assertEqual(kinds.description("Decisions").heading, "Решения")
        self.assertEqual(kinds.description("Open Questions").board, kinds.description("Открытые вопросы").board)
        self.assertTrue(kinds.description("Tasks before the release").who)
        self.assertTrue(minutes.for_analyst("For the analyst"))
        self.assertFalse(minutes.for_analyst("Decisions"))

    def test_english_items_who_and_tentative(self):
        _, sections = minutes.sections(ENGLISH)
        remarks, decisions = sections[1], sections[2]
        self.assertEqual(
            [(p.text, p.who) for p in minutes.items(remarks)],
            [("The report has no total per warehouse, a total for each warehouse is needed.", "Igor, 20.10.2026.")],
        )
        self.assertEqual(
            [(p.text, p.who) for p in minutes.items(decisions)],
            [
                ("The price comes from the price list.", "Tentative, Anna will confirm with the chief accountant."),
                ("The quantity comes from the file.", ""),
            ],
        )
        (self.folder / minutes.FILE).write_text(ENGLISH, encoding="utf-8")
        self.assertEqual(minutes.result(self.folder), "2 решения · 1 задача · 1 вопрос · 1 замечание")

    def test_english_items_go_to_the_board(self):
        self._say("Let's look at the stock import.")
        (self.folder / minutes.FILE).write_text(ENGLISH, encoding="utf-8")
        board = [(z.text, z.who, z.kind, z.due) for z in tasks.tasks(self.recordings)]
        self.assertEqual(
            [(text, who, kind) for text, who, kind, _ in board],
            [
                ("The report has no total per warehouse, a total for each warehouse is needed.", "Igor", tasks.TASK),
                ("The price comes from the price list.", "Anna will confirm with the chief accountant", tasks.DECISION),
                ("Put the import on the test database.", "Igor", tasks.TASK),
                ("How to convert boxes to pieces.", "Anna will ask the warehouse", tasks.QUESTION),
            ],
        )
        self.assertEqual(str(board[2][3]), "2026-10-16")

    def test_english_email_and_knowledge_base(self):
        email, base = minutes.for_email(ENGLISH), minutes.for_knowledge_base(ENGLISH)
        for text in (email, base):
            for extra in ("[05:40]", "For the analyst", "duplicated"):
                self.assertNotIn(extra, text)
            self.assertIn("2. The quantity comes from the file.\n", text)
        self.assertIn("Hello,", email)
        self.assertNotIn("Transcript -", email)
        self.assertNotIn("Hello,", base)
        self.assertNotIn("Here are the minutes", base)
        self.assertIn("Transcript - 2026-10-14_15-00", base)
        self.assertEqual(minutes.participants("Participants - Dmitry, Anna"), ("Participants", "Dmitry, Anna"))
        self.assertEqual(minutes.participants("Участники - Дмитрий"), ("Участники", "Дмитрий"))
        self.assertIsNone(minutes.participants("Hello,"))


if __name__ == "__main__":
    unittest.main()
