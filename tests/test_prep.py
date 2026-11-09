# SPDX-License-Identifier: GPL-3.0-only
"""Meeting prep with AI: what goes to the model (project details, meeting kind, next meeting in the calendar, the prep
and open questions), how the answer is added to the prep without repeats, failures; the page agenda from the past
collected once is the same as the board agenda."""

import datetime
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from knotes import catalog, ics_calendar, minutes, notes, prep, projects, providers, settings, spending, tasks
from knotes import transcript
from knotes.language import tr

MINUTES = """# Итоги

## Решения

1. Цена берется из прайса в базе. Предварительно, Анна подтвердит. [05:40]

## Задачи

1. Доделать загрузку. Игорь, 16.10.2026. [09:10]

## Открытые вопросы

1. Как пересчитывать коробки. Анна уточнит у склада. [09:18]
"""
ANSWER = """```text
Вопросы к встрече:
1. Как сейчас - Как вы сейчас ведете остатки по двум складам?
- Сроки препрода
Исключения - как пересчитывать коробки?
**Чек-лист**
Объемы - Сколько позиций номенклатуры?
Объемы  -  сколько позиций номенклатуры
До встречи - Запросить копию базы.

```"""


def _answer(text: str = ANSWER, cost: float = 0.004) -> tuple[int, str]:
    return 200, json.dumps(
        {"choices": [{"message": {"content": text}, "finish_reason": "stop"}], "usage": {"cost": cost}}
    )


class TestPrep(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.recordings = pathlib.Path(self._tmp.name)
        self.openrouter = providers.find("openrouter", "протоколы")
        self.params = minutes.Params(self.openrouter, "к", self.openrouter.model, "Инструкция протокола")

    def tearDown(self):
        self._tmp.cleanup()

    def _meeting(self, name: str = "2026-10-14_15-00", title: str = "Загрузка остатков") -> pathlib.Path:
        folder = self.recordings / name
        folder.mkdir()
        transcript.save(
            folder,
            [{"начало": 5.0, "конец": 9.0, "кто": "Я", "текст": "Смотрим."}],
            {"длительность": 60, "провайдер": "x", "стоимость": 0.1},
            [],
        )
        (folder / minutes.FILE).write_text(MINUTES, encoding="utf-8")
        (folder / notes.FILE).write_text("## Повестка\n\n- [x] Сроки\n- [ ] Кто подписывает акты\n", encoding="utf-8")
        catalog.set_(folder, title=title, project="Склад")
        return folder

    def _suggest(self, answer: tuple[int, str] | None = None) -> tuple[prep.Suggestion, dict]:
        with mock.patch.object(providers, "_send_json", return_value=answer or _answer()) as sending:
            result = prep.suggest(self.recordings, "Склад", self.params)
        return result, sending.call_args.args[1]

    def test_personal_data_hidden_from_a_cloud_model(self):
        """With hiding on, a phone in "About the project" reaches a cloud model as a label and comes back as the number
        in the prep; a model on this computer gets the text as is."""
        projects.set_(self.recordings, "Склад", projects.ProjectNotes("Телефон склада +7 913 555-12-34."))
        result, body = self._suggest(_answer("До встречи - позвонить на склад [ТЕЛЕФОН_1]."))
        content = body["messages"][1]["content"]
        self.assertNotIn("555-12-34", content)
        self.assertIn("[ТЕЛЕФОН_1]", content)
        self.assertEqual(result.lines, ("До встречи - позвонить на склад +7 913 555-12-34.",))
        self.params = minutes.Params(providers.find("lmstudio", "протоколы"), "", "модель", "И")
        _, body = self._suggest(_answer("До встречи - позвонить."))
        self.assertIn("+7 913 555-12-34", body["messages"][1]["content"])

    def test_cost_goes_to_the_spending_log_under_the_project(self):
        self._suggest(_answer(cost=0.004))
        expenses = spending.list_(self.recordings, [])
        self.assertEqual([(e.meeting, e.project, e.how_many) for e in expenses], [("", "Склад", 0.004)])
        self.assertEqual(expenses[0].title, tr("Meeting prep"))

    def test_model_gets_project_kind_calendar_prep_and_open_questions(self):
        self._meeting()
        projects.set_(self.recordings, "Склад", projects.ProjectNotes("УТ 11.5, два склада.\nКто утверждает цены?"))
        projects.set_kind(self.recordings, "Склад", "Обследование")
        projects.set_for_meeting(self.recordings, "Склад", "Сроки препрода")
        events = [
            ics_calendar.Event("Планерка", datetime.datetime(2026, 10, 27, 9), datetime.datetime(2026, 10, 27, 10)),
            ics_calendar.Event(
                "Загрузка остатков", datetime.datetime(2026, 10, 28, 10), datetime.datetime(2026, 10, 28, 11)
            ),
        ]
        with (
            mock.patch.object(settings, "key", return_value="https://calendar.example/ics"),
            mock.patch.object(ics_calendar, "upcoming", return_value=events),
        ):
            _, body = self._suggest()
        system, user = body["messages"]
        self.assertEqual(system, {"role": "system", "content": prep.INSTRUCTION.read_text(encoding="utf-8")})
        self.assertEqual(user["role"], "user")
        input_ = user["content"]
        for part in (
            "Проект - Склад",
            "Прошлых встреч по проекту - 1",
            "Ближайшая встреча - Загрузка остатков, 28.10.2026 10:00",  # a series of the project's meetings
            "Язык ответа - русский",
            "Вид встречи - Обследование",
            "- Как сейчас.",
            "О проекте\nУТ 11.5, два склада.\nКто утверждает цены?",
            "Уже в подготовке\n- Сроки препрода",
            "- Кто подписывает акты",  # not discussed last time
            "- Как пересчитывать коробки.",  # open question from the board
            "- Цена берется из прайса в базе.",  # decision awaiting confirmation
        ):
            self.assertIn(part, input_)
        self.assertNotIn("Доделать загрузку", input_)  # tasks are not questions
        self.assertNotIn("Планерка", input_)
        self.assertEqual(body["model"], self.openrouter.model)
        self.assertEqual(body["provider"], {"data_collection": "deny"})

    def test_first_meeting_kind_from_calendar_title_and_project_name(self):
        """No history: the meeting is found by the project name in the calendar title, its kind by the title."""
        event = ics_calendar.Event(
            "Обследование: склад", datetime.datetime(2026, 10, 28, 10), datetime.datetime(2026, 10, 28, 11)
        )
        with (
            mock.patch.object(settings, "key", return_value="https://calendar.example/ics"),
            mock.patch.object(ics_calendar, "upcoming", return_value=[event]),
        ):
            _, body = self._suggest()
        input_ = body["messages"][1]["content"]
        self.assertIn("Ближайшая встреча - Обследование: склад, 28.10.2026 10:00", input_)
        self.assertIn("Вид встречи - Обследование", input_)
        self.assertIn("Прошлых встреч по проекту - 0", input_)
        self.assertNotIn("Уже в подготовке", input_)
        _, body = self._suggest()  # no calendar link: a working meeting, no next meeting
        self.assertIn("Вид встречи - Рабочая встреча", body["messages"][1]["content"])
        self.assertNotIn("Ближайшая встреча", body["messages"][1]["content"])

    def test_calendar_failure_does_not_stop_prep(self):
        with (
            mock.patch.object(settings, "key", return_value="https://calendar.example/ics"),
            mock.patch.object(ics_calendar, "upcoming", side_effect=providers.NetworkError("нет сети")),
            self.assertLogs("knotes.prep", "WARNING"),
        ):
            result, body = self._suggest()
        self.assertNotIn("Ближайшая встреча", body["messages"][1]["content"])
        self.assertTrue(result.lines)

    def test_answer_language_from_project_details(self):
        projects.set_(self.recordings, "Склад", projects.ProjectNotes("Two warehouses, stock is kept in Excel."))
        _, body = self._suggest()
        input_ = body["messages"][1]["content"]
        self.assertIn("Язык ответа - английский", input_)
        self.assertIn("- Decisions («Решения»)", input_)
        for texts, code in (
            (("", ""), "ru"),
            (("", "Check the stock"), "en"),  # no details: the prep tells the language
            (("Склад, УТ 11.5, Excel", "Check"), "ru"),
            (("ok ок", ""), "ru"),  # a tie
        ):
            with self.subTest(texts=texts):
                self.assertEqual(prep.answer_language(*texts).code, code)

    def test_answer_lines_without_repeats_and_marks(self):
        self._meeting()
        projects.set_for_meeting(self.recordings, "Склад", "- Сроки препрода")
        result, _ = self._suggest()
        self.assertEqual(
            result.lines,
            (
                "Как сейчас - Как вы сейчас ведете остатки по двум складам?",
                "Объемы - Сколько позиций номенклатуры?",
                "До встречи - Запросить копию базы.",
            ),
        )
        self.assertAlmostEqual(result.cost, 0.004)

    def test_addition_keeps_user_text(self):
        lines = ("Как сейчас - Кто вносит остатки?", "До встречи - Запросить копию базы.")
        self.assertEqual(
            prep.addition("", lines), "Как сейчас - Кто вносит остатки?\nДо встречи - Запросить копию базы."
        )
        self.assertEqual(
            prep.addition("Сроки\nкто вносит остатки", lines), "\nДо встречи - Запросить копию базы."
        )  # the user typed the question meanwhile
        self.assertEqual(prep.addition("Сроки\n", lines[1:]), "До встречи - Запросить копию базы.")
        self.assertEqual(
            prep.addition("Сроки", lines, known=["Запросить копию базы"]), "\nКак сейчас - Кто вносит остатки?"
        )
        self.assertEqual(prep.addition("Сроки", lines[1:], known=["До встречи - запросить копию базы"]), "")

    def test_answer_for_a_closed_page_goes_to_saved_prep(self):
        projects.set_for_meeting(self.recordings, "Склад", "Сроки")
        prep.add_saved(self.recordings, "Склад", ("Сроки", "Объемы - Сколько складов?"))
        self.assertEqual(projects.for_meeting(self.recordings, "Склад"), "Сроки\nОбъемы - Сколько складов?")

    def test_failures_are_plain_and_nothing_is_lost(self):
        projects.set_for_meeting(self.recordings, "Склад", "Сроки")
        own_server = providers.find("свой", "протоколы")
        for params in (
            minutes.Params(self.openrouter, "", self.openrouter.model, "И"),  # no key
            minutes.Params(own_server, "", "", "И"),  # no address or model
        ):
            with self.subTest(service=params.service.id), mock.patch.object(providers, "_send_json") as sending:
                with self.assertRaises(providers.ProviderError):
                    prep.suggest(self.recordings, "Склад", params)
                sending.assert_not_called()
        for answer in ((500, '{"error": {"message": "overloaded"}}'), _answer("   ")):
            with self.subTest(answer=answer[0]), self.assertRaises(providers.ProviderError):
                self._suggest(answer)
        self.assertEqual(projects.for_meeting(self.recordings, "Склад"), "Сроки")

    def test_page_agenda_from_past_collected_once_is_board_agenda(self):
        self._meeting("2026-10-07_10-00", "Старая встреча")
        self._meeting()
        projects.set_(self.recordings, "Склад", projects.ProjectNotes("ЗУП 3.1.\nКто подписывает акты?"))
        projects.set_for_meeting(self.recordings, "Склад", "- Сроки препрода\nКак пересчитывать коробки?\n\n")
        meetings = [v for v in catalog.list_(self.recordings) if v.project == "Склад"]
        past = prep.past(meetings, tasks.tasks(self.recordings, "Склад"))
        self.assertEqual(past.meetings, 2)
        page = prep.agenda(
            projects.for_meeting(self.recordings, "Склад"), projects.notes(self.recordings, "Склад").text, past
        )
        self.assertEqual(page, tasks.agenda(self.recordings, "Склад"))
        self.assertEqual(
            [p.text for p in page],
            ["Сроки препрода", "Как пересчитывать коробки?", "Кто подписывает акты?", "Цена берется из прайса в базе."],
        )
        self.assertEqual(prep.agenda("", "", prep.Past()), [])


if __name__ == "__main__":
    unittest.main()
