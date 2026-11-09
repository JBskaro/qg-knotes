# SPDX-License-Identifier: GPL-3.0-only
"""Meeting kinds: own minutes sections for discovery, demo and others, choosing the kind, the "Tentative" note on
decisions, remarks and tentative decisions on the board."""

import pathlib
import tempfile
import unittest

from knotes import tasks, catalog, projects, providers, minutes, transcript, kinds

VIEW = """# Итоги встречи 14.10.2026 по загрузке остатков

Участники - Дмитрий, Анна

## Что показали

1. Загрузку остатков из файла склада. [01:10]

## Замечания

1. В отчете нет итога по складу, нужен итог по каждому складу. Игорь, 20.10.2026. [03:20]
2. Колонку «Цена» не показывать. [04:00]

## Решения

1. Цена берется из прайса в базе. Предварительно, Анна подтвердит у главного бухгалтера. [05:40]
2. Количество берется из файла. [06:10]

## Задачи

1. Выложить загрузку на тестовую базу. Игорь, 16.10.2026. [09:10]
"""


class TestKinds(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.recordings = pathlib.Path(self._tmp.name)
        self.folder = self.recordings / "2026-10-14_15-00"
        self.folder.mkdir()
        transcript.save(
            self.folder,
            [{"начало": 2.0, "конец": 4.0, "кто": "Я", "текст": "Смотрим загрузку."}],
            {"длительность": 600, "провайдер": "x", "стоимость": 0.1},
            [],
        )
        catalog.set_(self.folder, title="Загрузка остатков", project="Склад")

    def tearDown(self):
        self._tmp.cleanup()

    def test_each_kind_describes_all_sections(self):
        names = [v.name for v in kinds.kinds()]
        self.assertEqual(names[0], kinds.WORKING)
        self.assertEqual(len(names), len(set(names)))
        for kind in kinds.kinds():
            for heading in kind.sections + kinds.SHARED:
                with self.subTest(kind=kind.name, section=heading):
                    self.assertIn(heading, kinds.descriptions())
        self.assertTrue(kinds.description("Задачи (до релиза)").who)  # a heading with a qualifier is the same section
        self.assertEqual(kinds.description("Прочее"), kinds.Description("Прочее", ""))

    def test_meeting_kind_own_project_or_working(self):
        self.assertEqual(kinds.meeting_kind(self.folder).name, kinds.WORKING)
        projects.set_(self.recordings, "Склад", projects.ProjectNotes("У клиента ЗУП 3.1."))
        projects.set_kind(self.recordings, "Склад", "Обследование")
        self.assertEqual(kinds.meeting_kind(self.folder).name, "Обследование")
        self.assertEqual(projects.notes(self.recordings, "Склад").text, "У клиента ЗУП 3.1.")  # notes are intact
        catalog.set_(self.folder, kind="Показ доработки")
        self.assertEqual(kinds.meeting_kind(self.folder).name, "Показ доработки")
        catalog.set_(self.folder, kind="Вид, которого больше нет")
        self.assertEqual(kinds.meeting_kind(self.folder).name, kinds.WORKING)
        projects.set_(self.recordings, "Склад", projects.ProjectNotes(""))
        self.assertEqual(projects.kind(self.recordings, "Склад"), "Обследование")  # kind stays without notes
        projects.set_kind(self.recordings, "Склад", "")
        self.assertNotIn("Склад", (self.recordings / projects.FILE).read_text(encoding="utf-8"))

    def test_model_gets_kind_sections_in_order(self):
        catalog.set_(self.folder, kind="Обучение")
        s = providers.find("openrouter", "протоколы")
        input_ = minutes.input_(self.folder, minutes.Params(s, "к", s.model, "И"))
        self.assertIn("Вид встречи - Обучение. Пользователям показывают, как работать в программе", input_)
        order = [
            input_.index(f"- {z}. Пункт ")
            for z in (
                "Что показали",
                "Вопросы пользователей",
                "Замечания",
                "Задачи",
                "Открытые вопросы",
                "Следующая встреча",
                "Для аналитика",
            )
        ]
        self.assertEqual(order, sorted(order))
        self.assertIn("<Вопрос> - <ответ>. [мм:сс]", input_)
        self.assertIn("В инструкцию", input_)  # the kind instruction
        self.assertNotIn("- Решения. Пункт ", input_)
        self.assertLess(input_.index("Вид встречи"), input_.index("[00:02] Я: Смотрим"))

    def test_remarks_and_tentative_decision(self):
        _, sections = minutes.sections(VIEW)
        remarks, decisions = sections[1], sections[2]
        self.assertEqual(
            [(p.text, p.who) for p in minutes.items(remarks)],
            [
                ("В отчете нет итога по складу, нужен итог по каждому складу.", "Игорь, 20.10.2026."),
                ("Колонку «Цена» не показывать.", ""),
            ],
        )
        self.assertEqual(
            [(p.text, p.who) for p in minutes.items(decisions)],
            [
                ("Цена берется из прайса в базе.", "Предварительно, Анна подтвердит у главного бухгалтера."),
                ("Количество берется из файла.", ""),
            ],
        )
        (self.folder / minutes.FILE).write_text(VIEW, encoding="utf-8")
        self.assertEqual(minutes.result(self.folder), "2 решения · 1 задача · 2 замечания")

    def test_board_has_remarks_and_pending_decisions(self):
        (self.folder / minutes.FILE).write_text(VIEW, encoding="utf-8")
        board = [(z.text, z.who, z.kind) for z in tasks.tasks(self.recordings)]
        self.assertEqual(
            board,
            [
                ("В отчете нет итога по складу, нужен итог по каждому складу.", "Игорь", tasks.TASK),
                ("Колонку «Цена» не показывать.", "", tasks.TASK),
                ("Цена берется из прайса в базе.", "Анна подтвердит у главного бухгалтера", tasks.DECISION),
                ("Выложить загрузку на тестовую базу.", "Игорь", tasks.TASK),
            ],
        )


if __name__ == "__main__":
    unittest.main()
