# SPDX-License-Identifier: GPL-3.0-only
"""MCP tools for meetings: list, search, minutes, transcript, open items, spending and edits."""

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from knotes import ai_tools
from knotes import tasks, notes, catalog, projects, minutes, transcript
from knotes import settings
from knotes.mcp import ToolError

MINUTES = """# Итоги встречи 20.10.2026 по загрузке

Добрый день!

Участники - Дмитрий, Анна

## Решения

1. Цена берется из прайса. [00:05]

## Задачи

1. Доделать загрузку. Игорь, 22.10.2026. [00:25]

## Открытые вопросы

1. Как пересчитывать коробки. Анна уточнит. [00:45]

## Для аналитика

1. К задаче 1. Возможно задвоение. [00:25]
"""


def _call(tool: str, **arguments: object) -> dict:
    return ai_tools.TOOLS[tool].action(**arguments)


class TestTools(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.recordings = pathlib.Path(self._tmp.name)
        patcher = mock.patch.object(
            settings, "load", return_value=settings.Settings(recordings_folder=str(self.recordings))
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.first = self._meeting(
            "2026-10-20_10-00",
            "Склад",
            0.1,
            [("Я", "Цену берем из прайса в базе."), ("Собеседник 1", "Да, из прайса.")],
        )
        (self.first / minutes.FILE).write_text(MINUTES, encoding="utf-8")
        catalog.set_(self.first, minutes_cost=0.2, minutes_model="anthropic/claude-sonnet-5.5")
        transcript.set_names(self.first, {"Собеседник 1": "Анна"})
        self.second = self._meeting("2026-10-21_15-00", "Отчеты", 0.05, [("Я", "Смотрим отчет по продажам.")])

    def tearDown(self):
        self._tmp.cleanup()

    def _meeting(self, name: str, project: str, price: float, replies: list[tuple[str, str]]) -> pathlib.Path:
        folder = self.recordings / name
        folder.mkdir()
        phrases = [
            {"начало": n_ * 20.0 + 5, "конец": n_ * 20.0 + 15, "кто": who, "текст": text}
            for n_, (who, text) in enumerate(replies)
        ]
        transcript.save(
            folder,
            phrases,
            {"длительность": 600, "провайдер": "OpenRouter, elevenlabs/scribe-v2", "стоимость": price},
            [],
        )
        catalog.set_(folder, project=project)
        return folder

    def test_list_newest_first_and_filters(self):
        result = _call("list_meetings")
        self.assertEqual([v["встреча"] for v in result["встречи"]], ["2026-10-21_15-00", "2026-10-20_10-00"])
        first = result["встречи"][1]
        self.assertEqual(
            (first["проект"], first["участники"], first["протокол"], first["длительность_мин"]),
            ("Склад", ["Я", "Анна"], "1 решение · 1 задача · 1 вопрос", 10),
        )
        self.assertEqual(len(_call("list_meetings", project="Склад")["встречи"]), 1)
        self.assertEqual(len(_call("list_meetings", date_from="2026-10-21")["встречи"]), 1)
        self.assertEqual(len(_call("list_meetings", date_to="2026-10-20")["встречи"]), 1)
        with self.assertRaises(ToolError):
            _call("list_meetings", date_from="21.10.2026")

    def test_search_with_snippets_and_names(self):
        result = _call("search_meetings", query="прайс")
        self.assertEqual([n_["встреча"] for n_ in result["найдено"]], ["2026-10-20_10-00"])
        self.assertEqual(
            result["найдено"][0]["отрывки"], ["[00:05] Я: Цену берем из прайса в базе.", "[00:25] Анна: Да, из прайса."]
        )

    def test_minutes_in_three_views(self):
        full = _call("get_protocol", meeting="2026-10-20_10-00")
        self.assertIn("## Для аналитика", full["текст"])
        email = _call("get_protocol", meeting="2026-10-20_10-00", view="письмо")["текст"]
        base = _call("get_protocol", meeting="2026-10-20_10-00", view="база_знаний")["текст"]
        for text in (email, base):
            self.assertNotIn("Для аналитика", text)
            self.assertNotIn("[00:05]", text)
        self.assertIn("Добрый день!", email)
        self.assertNotIn("Добрый день!", base)
        with self.assertRaisesRegex(ToolError, "compose_protocol"):
            _call("get_protocol", meeting="2026-10-21_15-00")

    def test_transcript_in_pieces_and_by_person(self):
        whole = _call("get_transcript", meeting="2026-10-20_10-00")
        self.assertEqual(whole["текст"], "[00:05] Я: Цену берем из прайса в базе.\n[00:25] Анна: Да, из прайса.")
        self.assertEqual(_call("get_transcript", meeting="2026-10-20_10-00", from_time="00:20")["строк"], 1)
        self.assertEqual(
            _call("get_transcript", meeting="2026-10-20_10-00", speaker="Анна")["текст"], "[00:25] Анна: Да, из прайса."
        )
        with self.assertRaises(ToolError):
            _call("get_transcript", meeting="2026-10-20_10-00", to_time="пять минут")

    def test_task_board_states_own_tasks_and_notes(self):
        items = _call("list_open_items")["пункты"]
        self.assertEqual(
            [(p["текст"], p["кто"], p["срок"], p["вид"], p["основание"]["время"]) for p in items],
            [
                ("Доделать загрузку.", "Игорь", "2026-10-22", "задача", "00:25"),
                ("Как пересчитывать коробки.", "Анна уточнит", None, "вопрос", "00:45"),
            ],
        )
        self.assertEqual(items[0]["основание"]["встреча"], "2026-10-20_10-00")
        self.assertEqual(_call("list_open_items", project="Отчеты")["пункты"], [])
        self.assertEqual(_call("set_task_status", task=items[0]["ключ"], status="готово")["состояние"], "готово")
        self.assertEqual(len(_call("list_open_items")["пункты"]), 1)  # the done one is gone
        self.assertEqual(len(_call("list_open_items", include_done=True)["пункты"]), 2)
        for key, status in ((items[0]["ключ"], "потом"), ("нет такой", "готово")):
            with self.subTest(key=key), self.assertRaises(ToolError):
                _call("set_task_status", task=key, status=status)
        custom = _call("add_task", text="Согласовать выгрузку", assignee="Дмитрий", due="2026-10-25", project="Склад")
        self.assertEqual(
            (custom["основание"]["название"], custom["срок"], custom["кто"], custom["проект"]),
            ("добавлена вручную", "2026-10-25", "Дмитрий", "Склад"),
        )
        projects.set_(self.recordings, "Склад", projects.ProjectNotes("У клиента ЗУП 3.1."))
        self.assertEqual(_call("get_project_notes", project="Склад")["заметки"], "У клиента ЗУП 3.1.")

    def test_meeting_notes_and_preparation(self):
        notes.add(self.first, 65, "Решение - цена из прайса")
        result = _call("get_meeting_notes", meeting="2026-10-20_10-00")
        self.assertEqual(result["строки"], [{"время": "01:05", "текст": "Решение - цена из прайса"}])
        projects.set_for_meeting(self.recordings, "Склад", "Сроки препрода")
        preparation = _call("get_project_notes", project="Склад")
        self.assertEqual(preparation["к_следующей_встрече"], "Сроки препрода")
        self.assertIn("Сроки препрода", preparation["повестка"])
        self.assertIn("Как пересчитывать коробки.", preparation["повестка"])  # an open question from the board

    def test_card_edit_and_archive(self):
        question = _call("list_open_items")["пункты"][1]
        self.assertEqual(
            {p: question[p] for p in ("описание", "метки", "приоритет", "архив", "чек_лист")},
            {"описание": "", "метки": [], "приоритет": "", "архив": False, "чек_лист": []},
        )
        key = question["ключ"]
        result = _call(
            "update_task",
            task=key,
            text="Как пересчитывать коробки на складе",
            description="По весу",
            assignee="Анна",
            due="2026-11-01",
            priority="высокий",
            labels=["Склад", "Склад", "Цены"],
            status="в работе",
        )
        self.assertEqual(
            (
                result["текст"],
                result["описание"],
                result["кто"],
                result["срок"],
                result["приоритет"],
                result["метки"],
                result["состояние"],
                result["ключ"],
            ),
            (
                "Как пересчитывать коробки на складе",
                "По весу",
                "Анна",
                "2026-11-01",
                "высокий",
                ["Склад", "Цены"],
                "в работе",
                key,
            ),
        )
        result = _call("update_task", task=key, due="", priority="")
        self.assertEqual((result["срок"], result["приоритет"], result["описание"]), (None, "", "По весу"))
        self.assertEqual((self.first / minutes.FILE).read_text(encoding="utf-8"), MINUTES)  # the minutes are the same
        tasks.edit(
            self.recordings, key, tasks.Edit(archive=True, checklist=(tasks.ChecklistItem("Спросить склад", True),))
        )
        self.assertNotIn(key, [p["ключ"] for p in _call("list_open_items")["пункты"]])  # an archived one is not open
        archived = next(p for p in _call("list_open_items", include_done=True)["пункты"] if p["ключ"] == key)
        self.assertEqual(
            (archived["архив"], archived["чек_лист"]), (True, [{"текст": "Спросить склад", "сделан": True}])
        )
        for fields, word in (
            ({"task": "нет такой", "text": "Х"}, "list_open_items"),
            ({"task": key, "priority": "важный"}, "Приоритет"),
            ({"task": key, "due": "01.11.2026"}, "due"),
            ({"task": key, "color": "red"}, "color"),
            ({"task": key}, "text"),
            ({"task": key, "text": " "}, "текст"),
        ):
            with self.subTest(fields=fields), self.assertRaisesRegex(ToolError, word):
                _call("update_task", **fields)

    def test_costs(self):
        result = _call("get_costs")
        self.assertEqual((result["всего"], result["речь"], result["протоколы"]), (0.35, 0.15, 0.2))
        self.assertEqual([m["модель"] for m in result["по_моделям"]], ["ElevenLabs Scribe v2", "Claude Sonnet 5.5"])
        self.assertEqual(result["по_встречам"][1]["всего"], 0.3)

    def test_names_title_and_project(self):
        result = _call("set_speaker_names", meeting="2026-10-20_10-00", names={"Собеседник 1": "Анна Петрова"})
        self.assertEqual([u["имя"] for u in result["участники"]], ["Я", "Анна Петрова"])
        for names, word in (({"Собеседник 7": "Петр"}, "Собеседник 7"), ({"Я": "Дмитрий"}, "настройках")):
            with self.subTest(names=names), self.assertRaisesRegex(ToolError, word):
                _call("set_speaker_names", meeting="2026-10-20_10-00", names=names)
        card = _call("set_meeting_info", meeting="2026-10-21_15-00", title="Отчет по продажам", project="")
        self.assertEqual((card["название"], card["проект"]), ("Отчет по продажам", ""))

    def test_edit_and_compose_minutes_with_previous_kept(self):
        _call("update_protocol", meeting="2026-10-20_10-00", text="# Итоги\n\n## Решения\n\n1. Новое. [00:05]\n")
        self.assertEqual((self.first / "протокол.прежний.md").read_text(encoding="utf-8"), MINUTES)
        self.assertEqual(_call("list_meetings", project="Склад")["встречи"][0]["протокол"], "1 решение")

        def compose(folder: pathlib.Path, _params: object, keep: object) -> pathlib.Path:
            keep(folder)
            (folder / minutes.FILE).write_text("# Черновик\n", encoding="utf-8")
            return folder / minutes.FILE

        with (
            mock.patch.object(minutes, "compose", side_effect=compose),
            mock.patch.object(minutes, "params", return_value=mock.Mock(service=mock.Mock(assistant=False))),
        ):
            result = _call("compose_protocol", meeting="2026-10-20_10-00")
            self.assertEqual((result["текст"], result["вид_встречи"]), ("# Черновик\n", "Рабочая встреча"))
            self.assertIn("Новое", (self.first / "протокол.прежний.md").read_text(encoding="utf-8"))
            result = _call("compose_protocol", meeting="2026-10-20_10-00", kind="Показ доработки")
            with self.assertRaisesRegex(ToolError, "kind"):
                _call("compose_protocol", meeting="2026-10-20_10-00", kind="Планерка")
        self.assertEqual(result["вид_встречи"], "Показ доработки")

    def test_foreign_folder_and_unknown_meeting(self):
        for name in ("..", "..\\..\\Windows", "нет такой", ""):
            with self.subTest(name=name), self.assertRaisesRegex(ToolError, "list_meetings"):
                _call("get_transcript", meeting=name)

    def test_config_for_claude(self):
        """The config starts this same program with --mcp; in development, Python with the path to the package."""
        server = json.loads(ai_tools.client_config())["mcpServers"]["qg-knotes"]
        self.assertEqual(server["args"][-1], "--mcp")
        self.assertTrue(server["command"].endswith("python.exe"))
        self.assertTrue((pathlib.Path(server["env"]["PYTHONPATH"]) / "knotes").is_dir())

    def test_previous_minutes_texts_rotate_and_kind_on_failure(self):
        for number in range(7):
            _call("update_protocol", meeting="2026-10-20_10-00", text=f"# Версия {number}")
        copies = sorted(p.name for p in self.first.glob("протокол.прежний*.md"))
        self.assertEqual(len(copies), ai_tools.COPIES)
        self.assertIn("Версия 5", (self.first / ai_tools.PREVIOUS).read_text(encoding="utf-8"))  # the last previous one
        with (
            mock.patch.object(minutes, "compose", side_effect=OSError("нет сети")),
            mock.patch.object(minutes, "params"),
            self.assertRaises(ToolError),
        ):
            _call("compose_protocol", meeting="2026-10-20_10-00", kind="Обследование")
        self.assertNotIn("вид", catalog.properties(self.first))  # not written: the meeting kind stays the same
        self.assertTrue(ai_tools.TOOLS["update_task"].describe()["annotations"]["destructiveHint"])

    def test_schemas_match_params(self):
        """Parameter names are in Latin letters (Claude accepts them so) and match those of the functions."""
        for name, tool in ai_tools.TOOLS.items():
            code = tool.action.__code__
            params = set(code.co_varnames[: code.co_argcount + code.co_kwonlyargcount])
            if name == "update_task":  # edit fields are named as in TASK_EDITS, there are more than five
                params |= set(ai_tools.TASK_EDITS)
            with self.subTest(name=name):
                self.assertRegex(name, r"^[a-z_]{1,64}$")
                for param in tool.params:
                    self.assertRegex(param, r"^[a-z_]+$")
                    self.assertIn(param, params)


if __name__ == "__main__":
    unittest.main()
