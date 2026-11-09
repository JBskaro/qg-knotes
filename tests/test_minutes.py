# SPDX-License-Identifier: GPL-3.0-only
"""Meeting minutes: what goes to the model, the draft in the meeting folder, the email and the knowledge base
text from the draft, a custom instruction."""

import datetime
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from knotes import notes, catalog, projects, providers, minutes, transcript, summary
from knotes import settings
from knotes import vocabulary

DRAFT = """# Итоги встречи 14.10.2026 по загрузке остатков

> Пометка, которую модель могла добавить сама.

Добрый день!

Направляем итоги встречи 14.10.2026 по загрузке остатков.

Участники - Дмитрий, Анна

## Решения

1. Цена берется из прайса в базе. [05:40]

## Задачи

1. Доделать загрузку. Игорь, 16.10.2026. [1:09:10]

Расшифровка - 2026-10-14_15-00, расшифровка.md

## Для аналитика

1. К задаче 1. Игорь предполагает задвоение. [11:10]
"""


def _answer(text: str, cost: float = 0.04, end: str = "stop") -> tuple[int, str]:
    return 200, json.dumps({"choices": [{"message": {"content": text}, "finish_reason": end}], "usage": {"cost": cost}})


class TestMinutes(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._temporary.name) / "2026-10-14_15-00"
        self.folder.mkdir()
        transcript.save(
            self.folder,
            [{"начало": 2.0, "конец": 4.0, "кто": "Я", "текст": "Смотрим загрузку."}],
            {"длительность": 60, "провайдер": "OpenRouter", "стоимость": 0.1},
            [],
        )
        catalog.set_(self.folder, title="Загрузка остатков", project="Склад")
        self.openrouter = providers.find("openrouter", "протоколы")
        self.params = minutes.Params(
            self.openrouter,
            "к",
            self.openrouter.model,
            "Инструкция",
            "Дмитрий",
            (vocabulary.Word("ЕГАИС", ("игаис",)),),
        )

    def tearDown(self):
        self._temporary.cleanup()

    def test_model_gets_meeting_info_and_transcript(self):
        input_ = minutes.input_(self.folder, self.params)
        for part in (
            "Дата встречи - 14.10.2026",
            "Название встречи - Загрузка остатков",
            "Проект - Склад",
            "Имя владельца - Дмитрий",
            "- ЕГАИС (может распознаться как игаис)",
            "[00:02] Я: Смотрим",
        ):
            self.assertIn(part, input_)
        self.assertNotIn("Заметки по проекту", input_)

    def test_project_and_meeting_notes_go_to_model(self):
        recordings = self.folder.parent
        projects.set_(recordings, "Склад", projects.ProjectNotes("Цены у клиента всегда из прайса."))
        notes.add(self.folder, 3, "Решение - цена из прайса")
        catalog.set_(self.folder, calendar_participants=["Анна Петрова", "Игорь Сидоров"])
        input_ = minutes.input_(self.folder, self.params)
        self.assertIn("Заметки по проекту", input_)
        self.assertIn("Цены у клиента всегда из прайса.", input_)
        self.assertIn("Заметки аналитика", input_)
        self.assertIn("[00:03] Решение - цена из прайса", input_)
        self.assertIn("Участники по календарю - Анна Петрова, Игорь Сидоров", input_)
        projects.set_(recordings, "Склад", projects.ProjectNotes("Цены у клиента всегда из прайса.", for_model=False))
        self.assertNotIn("Заметки по проекту", minutes.input_(self.folder, self.params))

    def test_draft_in_meeting_folder_and_price_in_its_cost(self):
        with mock.patch.object(
            providers, "_send_json", return_value=_answer("```markdown\n# Итоги\n\n1. Решение.\n```")
        ) as sending:
            file = minutes.compose(self.folder, self.params)
        self.assertEqual(file.read_text(encoding="utf-8"), "# Итоги\n\n1. Решение.\n")
        body = sending.call_args.args[1]
        self.assertEqual(body["messages"][0], {"role": "system", "content": "Инструкция"})
        self.assertEqual(body["provider"], {"data_collection": "deny"})
        meeting = catalog.meeting(self.folder)
        self.assertAlmostEqual(meeting.cost, 0.14)  # the whole meeting: speech recognition and minutes
        self.assertAlmostEqual(meeting.minutes_cost, 0.04)
        self.assertAlmostEqual(summary.result([meeting], None, datetime.date(2030, 1, 1)).minutes_list, 0.04)

    def test_does_not_send_without_transcript_key_or_address(self):
        (self.folder / transcript.TRANSCRIPT).unlink()
        with self.assertRaises(FileNotFoundError):
            minutes.compose(self.folder, self.params)
        transcript.collect(self.folder)
        no_model = minutes.Params(providers.find("свой", "протоколы"), "", "", "И")
        for params in (minutes.Params(self.openrouter, "", self.openrouter.model, "И"), no_model):
            with self.subTest(service=params.service.id), self.assertRaises(providers.ProviderError):
                minutes.compose(self.folder, params)

    def test_email_without_link_kb_without_greeting_both_without_draft_marks(self):
        email, base = minutes.for_email(DRAFT), minutes.for_knowledge_base(DRAFT)
        for text in (email, base):
            for extra in ("Пометка", "[05:40]", "[1:09:10]", "Для аналитика", "задвоение", "\n\n\n"):
                self.assertNotIn(extra, text)
            self.assertIn("1. Цена берется из прайса в базе.\n", text)
        self.assertIn("Добрый день!", email)
        self.assertNotIn("Расшифровка -", email)
        self.assertNotIn("Добрый день!", base)
        self.assertNotIn("Направляем итоги", base)
        self.assertIn("Расшифровка - 2026-10-14_15-00", base)
        self.assertTrue(minutes.no_diarization(email).startswith("Итоги встречи"))

    def test_sections_for_display_as_blocks(self):
        start, sections = minutes.sections(DRAFT)
        self.assertTrue(start.startswith("# Итоги встречи") and start.endswith("Участники - Дмитрий, Анна"))
        self.assertEqual(
            [(r.heading, r.item_count) for r in sections], [("Решения", 1), ("Задачи", 1), ("Для аналитика", 1)]
        )
        self.assertIn("Расшифровка - 2026-10-14_15-00", sections[1].text)
        self.assertEqual(minutes.sections("Просто текст без разделов"), ("Просто текст без разделов", []))

    def test_section_items_who_and_when_apart(self):
        """In tasks and questions the last phrase is who and when: it is highlighted so the eye catches it at once."""
        tasks = minutes.Section(
            "Задачи",
            "1. Доделать загрузку. Выложить на тест. Игорь, 16.10.2026. [1:09:10]\n"
            "2. Проверить коэффициент. [09:30]\n\nРасшифровка - папка",
            2,
        )
        self.assertEqual(
            minutes.items(tasks),
            [
                minutes.Item("1", "Доделать загрузку. Выложить на тест.", "Игорь, 16.10.2026.", 4150),
                minutes.Item("2", "Проверить коэффициент.", "", 570),
                minutes.Item("", "Расшифровка - папка"),
            ],
        )
        decisions = minutes.Section("Решения", "1. Цена из прайса. Количество из файла. [05:40]", 1)
        self.assertEqual(minutes.items(decisions), [minutes.Item("1", "Цена из прайса. Количество из файла.", "", 340)])

    def test_item_from_analyst_note_marked_and_email_without_sign(self):
        tasks = minutes.Section("Задачи", "1. Доделать загрузку. Игорь, 16.10.2026. [09:10] ✎", 1)
        self.assertEqual(
            minutes.items(tasks), [minutes.Item("1", "Доделать загрузку.", "Игорь, 16.10.2026.", 550, True)]
        )
        email = minutes.for_email("# Итоги\n\n## Задачи\n\n1. Доделать загрузку. Игорь. [09:10] ✎\n")
        self.assertIn("1. Доделать загрузку. Игорь.\n", email)
        self.assertNotIn("✎", email)

    def test_minutes_result_in_one_line(self):
        self.assertEqual(minutes.result(self.folder), "")  # no minutes yet
        (self.folder / minutes.FILE).write_text(DRAFT, encoding="utf-8")
        self.assertEqual(minutes.result(self.folder), "1 решение · 1 задача")  # minutes.FOR_ANALYST is not counted

    def test_own_instruction_and_back_to_default(self):
        with (
            tempfile.TemporaryDirectory() as data,
            mock.patch.object(settings, "data_folder", return_value=pathlib.Path(data)),
        ):
            default_text = minutes.instruction()
            self.assertIn("Протокол встречи по расшифровке", default_text)
            minutes.set_instruction("Своя инструкция")
            self.assertEqual(minutes.instruction(), "Своя инструкция\n")
            minutes.set_instruction(default_text)
            self.assertFalse(minutes.instruction_file().exists())

    def test_params_from_settings(self):
        with mock.patch.object(settings, "address_key", return_value="ключ"):
            params = minutes.params(settings.Settings(my_name=" Дмитрий ", minutes_model_id="qwen3"))
        self.assertEqual((params.model, params.my_name, params.key), ("qwen3", "Дмитрий", "ключ"))


class TestModelAnswer(unittest.TestCase):
    def _ask(self, answer: tuple[int, str], body: dict | None = None) -> tuple[str, float]:
        with mock.patch.object(providers, "_send_json", return_value=answer) as sending:
            result = providers.model_answer(providers.find("openrouter", "протоколы"), "к", body or {})
        self.assertEqual(sending.call_args.args[2], "https://openrouter.ai/api/v1/chat/completions")
        return result

    def test_text_and_cost(self):
        self.assertEqual(self._ask(_answer("Итоги", 0.05)), ("Итоги", 0.05))

    def test_failures_in_plain_text(self):
        for answer, body, word in [
            ((404, "{}"), {"provider": {"data_collection": "deny"}}, "без хранения"),
            ((401, "{}"), {}, "ключ"),
            (_answer(" "), {}, "пустой"),
            (_answer("Итоги", end="length"), {}, "оборвал"),
        ]:
            with self.subTest(word=word), self.assertRaisesRegex(providers.ProviderError, word):
                self._ask(answer, body)

    def test_transcript_never_goes_over_http_to_foreign_server(self):
        with self.assertRaisesRegex(providers.ProviderError, "без https"):
            providers.model_answer(providers.connection("протоколы", "свой", "http://example.com/v1"), "", {})

    def test_own_server_without_key_gets_no_header(self):
        with mock.patch.object(providers, "run_program", return_value=(0, b"{}\n200", b"")) as startup:
            providers._curl("", [], "http://localhost:11434/v1/models")
        self.assertEqual(startup.call_args.args[1], b"")


if __name__ == "__main__":
    unittest.main()
