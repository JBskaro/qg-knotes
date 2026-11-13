# SPDX-License-Identifier: GPL-3.0-only
"""Task board and project notes: tasks from the minutes with their source, own tasks, card edits, labels, order in a
column, archive, filters."""

import dataclasses
import datetime
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from knotes import tasks, catalog, projects, minutes, transcript

MINUTES = """# Итоги

## Решения

1. Цена из прайса. [00:05]

## Задачи

1. Доделать загрузку. Игорь, 16.10.2026. [09:10]
2. Проверить коэффициент коробки. Дмитрий. [09:30]

## Открытые вопросы

1. Как пересчитывать коробки. Анна уточнит у склада. [09:18]

## Для аналитика

1. К задаче 1. Задвоение. [11:10]
"""
TODAY = datetime.date(2026, 10, 22)  # Thursday


class TestBoard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.recordings = pathlib.Path(self._tmp.name)
        self.meeting = self.recordings / "2026-10-14_15-00"
        self.meeting.mkdir()
        transcript.save(
            self.meeting,
            [{"начало": 5.0, "конец": 9.0, "кто": "Я", "текст": "Смотрим."}],
            {"длительность": 60, "провайдер": "OpenRouter, x", "стоимость": 0.1},
            [],
        )
        (self.meeting / minutes.FILE).write_text(MINUTES, encoding="utf-8")
        catalog.set_(self.meeting, title="Загрузка остатков", project="Склад")

    def tearDown(self):
        self._tmp.cleanup()

    def _by_text(self) -> dict[str, tasks.Task]:
        return {z.text: z for z in tasks.tasks(self.recordings)}

    def _file(self) -> dict:
        return json.loads((self.recordings / tasks.FILE).read_text(encoding="utf-8"))

    def test_tasks_and_questions_from_minutes_with_source(self):
        board = tasks.tasks(self.recordings)
        self.assertEqual(
            [(z.text, z.who, z.due, z.kind) for z in board],
            [
                ("Доделать загрузку.", "Игорь", datetime.date(2026, 10, 16), tasks.TASK),
                ("Проверить коэффициент коробки.", "Дмитрий", None, tasks.TASK),
                ("Как пересчитывать коробки.", "Анна уточнит у склада", None, tasks.QUESTION),
            ],
        )
        origin = board[0].origin
        self.assertEqual(
            (origin.folder, origin.title, origin.date, origin.second),
            (self.meeting, "Загрузка остатков", datetime.date(2026, 10, 14), 550),
        )
        self.assertEqual(board[0].project, "Склад")
        self.assertEqual(
            (
                board[0].description,
                board[0].labels,
                board[0].priority,
                board[0].order,
                board[0].archive,
                board[0].checklist,
                board[0].custom,
            ),
            ("", (), "", None, False, (), False),
        )
        self.assertTrue(board[0].overdue(TODAY))
        self.assertFalse(board[1].overdue(TODAY))  # no due date: not overdue
        question = dataclasses.replace(board[2], due=datetime.date(2026, 10, 1))
        self.assertFalse(question.overdue(TODAY))  # a question's date is when it is discussed

    def test_due_soon_only_for_task_within_week(self):
        task = tasks.tasks(self.recordings)[1]
        for days_count, soon in ((0, True), (7, True), (8, False), (-1, False), (None, False)):
            due = None if days_count is None else TODAY + datetime.timedelta(days=days_count)
            with self.subTest(due=due):
                self.assertEqual(dataclasses.replace(task, due=due).soon(TODAY), soon)
        self.assertFalse(dataclasses.replace(task, due=TODAY, status=tasks.DONE).soon(TODAY))
        self.assertFalse(dataclasses.replace(task, due=TODAY, kind=tasks.QUESTION).soon(TODAY))

    def test_state_kept_by_item_text(self):
        first = tasks.tasks(self.recordings)[0]
        tasks.edit(self.recordings, first.key, tasks.Edit(status=tasks.IN_PROGRESS))
        self.assertEqual(tasks.tasks(self.recordings)[0].status, tasks.IN_PROGRESS)
        (self.meeting / minutes.FILE).write_text(
            MINUTES.replace("1. Доделать", "2. Доделать").replace("2. Проверить", "1. Проверить"), encoding="utf-8"
        )
        by_text = {z.text: z.status for z in tasks.tasks(self.recordings)}  # the minutes were written again
        self.assertEqual(by_text["Доделать загрузку."], tasks.IN_PROGRESS)
        tasks.edit(self.recordings, first.key, tasks.Edit(status=tasks.DONE))
        self.assertFalse(tasks.tasks(self.recordings)[0].overdue(TODAY))  # a done task is not overdue
        with self.assertRaises(ValueError):
            tasks.edit(self.recordings, first.key, tasks.Edit(status="потом"))

    def test_minutes_item_edit_stored_apart_minutes_unchanged(self):
        first = tasks.tasks(self.recordings)[0]
        tasks.edit(
            self.recordings,
            first.key,
            tasks.Edit(
                text="  Доделать загрузку остатков  ",
                description="Из файла поставщика",
                who="Дмитрий",
                due=datetime.date(2026, 11, 1),
                labels=("Склад", "Срочно", "Склад"),
                priority="высокий",
                checklist=(tasks.ChecklistItem("Формат"), tasks.ChecklistItem("Проверка", True)),
            ),
        )
        edited_text = tasks.tasks(self.recordings)[0]
        self.assertEqual(
            (
                edited_text.key,
                edited_text.text,
                edited_text.description,
                edited_text.who,
                edited_text.due,
                edited_text.labels,
                edited_text.priority,
                edited_text.checklist,
            ),
            (
                first.key,
                "Доделать загрузку остатков",
                "Из файла поставщика",
                "Дмитрий",
                datetime.date(2026, 11, 1),
                ("Склад", "Срочно"),
                "высокий",
                (tasks.ChecklistItem("Формат", False), tasks.ChecklistItem("Проверка", True)),
            ),
        )
        self.assertEqual((self.meeting / minutes.FILE).read_text(encoding="utf-8"), MINUTES)
        self.assertEqual([m.name for m in tasks.labels(self.recordings)], ["Склад", "Срочно"])  # new ones created
        tasks.edit(self.recordings, first.key, tasks.Edit(due=tasks.NO_DUE, who="", description=""))
        edited_text = tasks.tasks(self.recordings)[0]
        self.assertEqual(
            (edited_text.due, edited_text.who, edited_text.description, edited_text.text),
            (None, "", "", "Доделать загрузку остатков"),
        )  # an emptied minutes item field stays empty
        (self.meeting / minutes.FILE).write_text(MINUTES.replace("1. Доделать", "3. Доделать"), encoding="utf-8")
        self.assertEqual(self._by_text()["Доделать загрузку остатков"].labels, ("Склад", "Срочно"))

    def test_invalid_edit_is_not_written(self):
        key = tasks.tasks(self.recordings)[0].key
        for edit_item in (tasks.Edit(text="  "), tasks.Edit(priority="важный"), tasks.Edit(status="потом")):
            with self.subTest(edit_item=edit_item), self.assertRaises(ValueError):
                tasks.edit(self.recordings, key, edit_item)
        self.assertFalse((self.recordings / tasks.FILE).exists())
        with self.assertRaises(ValueError):
            tasks.edit(self.recordings, tasks.OWN_PREFIX + "нет", tasks.Edit(text="Х"))

    def test_old_board_file_is_read(self):
        (self.recordings / tasks.FILE).parent.mkdir(parents=True)
        key = tasks.tasks(self.recordings)[1].key
        (self.recordings / tasks.FILE).write_text(
            json.dumps(
                {
                    "состояния": {key: "в работе"},
                    "свои": [
                        {
                            "ид": "ab12",
                            "текст": "Согласовать",
                            "кто": "Анна",
                            "срок": "2026-10-30",
                            "проект": "Склад",
                            "состояние": "готово",
                            "создана": "2026-10-20",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        by_text = self._by_text()
        self.assertEqual(by_text["Проверить коэффициент коробки."].status, tasks.IN_PROGRESS)
        custom = by_text["Согласовать"]
        self.assertEqual(
            (custom.key, custom.who, custom.due, custom.status, custom.labels, custom.priority, custom.custom),
            ("свои|ab12", "Анна", datetime.date(2026, 10, 30), tasks.DONE, (), "", True),
        )
        tasks.edit(self.recordings, custom.key, tasks.Edit(description="Письмом"))
        self.assertEqual(self._file()["состояния"], {key: "в работе"})  # existing data is not lost

    def test_own_tasks_project_edit_and_delete(self):
        custom = tasks.add(
            self.recordings, "Согласовать выгрузку", "Склад", tasks.Edit(who="Дмитрий", due=datetime.date(2026, 10, 25))
        )
        self.assertEqual(
            (custom.origin.folder, custom.origin.title, custom.origin.date, custom.who, custom.due),
            (None, tasks.MANUAL, datetime.date.today(), "Дмитрий", datetime.date(2026, 10, 25)),
        )
        tasks.edit(self.recordings, custom.key, tasks.Edit(status=tasks.DONE))
        self.assertEqual([z.status for z in tasks.tasks(self.recordings, "Склад") if z.key == custom.key], [tasks.DONE])
        tasks.edit(
            self.recordings,
            custom.key,
            tasks.Edit(text="Согласовать выгрузку цен", priority="срочный", due=tasks.NO_DUE),
        )
        self.assertEqual(
            [(z.text, z.priority, z.due) for z in tasks.tasks(self.recordings, "Склад") if z.custom],
            [("Согласовать выгрузку цен", "срочный", None)],
        )
        self.assertNotIn("правки", self._file())  # own tasks keep their fields themselves
        self.assertEqual(tasks.tasks(self.recordings, "Отчеты"), [])
        tasks.rename_project(self.recordings, "Склад", "Склад 2")
        self.assertEqual([z.text for z in tasks.tasks(self.recordings, "Склад 2")], ["Согласовать выгрузку цен"])
        tasks.delete(self.recordings, custom.key)
        self.assertEqual(len(tasks.tasks(self.recordings)), 3)
        with self.assertRaises(ValueError):
            tasks.delete(self.recordings, tasks.tasks(self.recordings)[0].key)  # minutes item: archive only
        with self.assertRaises(ValueError):
            tasks.add(self.recordings, " ")

    def test_archive(self):
        first = tasks.tasks(self.recordings)[0]
        tasks.edit(self.recordings, first.key, tasks.Edit(archive=True))
        self.assertTrue(tasks.tasks(self.recordings)[0].archive)
        tasks.edit(self.recordings, first.key, tasks.Edit(archive=False))
        self.assertFalse(tasks.tasks(self.recordings)[0].archive)
        self.assertNotIn(first.key, self._file().get("правки", {}))  # an edit with no differences is not stored

    def test_labels_create_rename_recolor_delete(self):
        storage = tasks.add_label(self.recordings, " Склад ")
        urgent = tasks.add_label(self.recordings, "Срочно", color=4)
        self.assertEqual((storage, urgent), (tasks.Label("Склад", 0), tasks.Label("Срочно", 4)))
        for name in ("Склад", "  "):
            with self.subTest(name=name), self.assertRaises(ValueError):
                tasks.add_label(self.recordings, name)
        first = tasks.tasks(self.recordings)[0]
        custom = tasks.add(self.recordings, "Согласовать", edit_item=tasks.Edit(labels=("Склад", "Срочно")))
        tasks.edit(self.recordings, first.key, tasks.Edit(labels=("Склад",)))
        tasks.rename_label(self.recordings, "Склад", "Остатки")
        self.assertEqual(
            {z.key: z.labels for z in tasks.tasks(self.recordings) if z.labels},
            {first.key: ("Остатки",), custom.key: ("Остатки", "Срочно")},
        )
        with self.assertRaises(ValueError):
            tasks.rename_label(self.recordings, "Остатки", "Срочно")
        tasks.rename_label(self.recordings, "Остатки", "Остатки")  # same name: nothing changes
        tasks.recolor_label(self.recordings, "Остатки", 9)
        self.assertEqual(tasks.labels(self.recordings), [tasks.Label("Остатки", 2), tasks.Label("Срочно", 4)])
        tasks.delete_label(self.recordings, "Остатки")
        self.assertEqual(tasks.labels(self.recordings), [tasks.Label("Срочно", 4)])
        self.assertEqual({z.key: z.labels for z in tasks.tasks(self.recordings) if z.labels}, {custom.key: ("Срочно",)})

    def test_order_in_column(self):
        custom = tasks.add(self.recordings, "Своя без срока")
        board = tasks.tasks(self.recordings)
        make = [z for z in board if z.status == tasks.TODO]
        self.assertEqual(
            [z.text for z in tasks.in_order(make, TODAY)],
            ["Доделать загрузку.", "Своя без срока", "Проверить коэффициент коробки.", "Как пересчитывать коробки."],
        )  # overdue first, then by due date, newer ones higher
        question = next(z.key for z in board if z.kind == tasks.QUESTION)
        tasks.reorder(self.recordings, tasks.IN_PROGRESS, [question, custom.key])
        tasks.reorder(self.recordings, tasks.IN_PROGRESS, [custom.key, question])  # dragged higher
        new_one = tasks.add(self.recordings, "Новая", edit_item=tasks.Edit(status=tasks.IN_PROGRESS))
        self.assertIsNone(new_one.order)

        def in_progress() -> list[str]:
            return [
                z.text
                for z in tasks.in_order(
                    [z for z in tasks.tasks(self.recordings) if z.status == tasks.IN_PROGRESS], TODAY
                )
            ]

        self.assertEqual(in_progress(), ["Новая", "Своя без срока", "Как пересчитывать коробки."])  # no place: on top
        tasks.add(self.recordings, "В конец", edit_item=tasks.Edit(status=tasks.IN_PROGRESS, order=5))
        tasks.edit(self.recordings, question, tasks.Edit(status=tasks.DONE))
        tasks.edit(self.recordings, question, tasks.Edit(status=tasks.IN_PROGRESS))  # came back: no place, to the top
        self.assertEqual(in_progress(), ["Новая", "Как пересчитывать коробки.", "Своя без срока", "В конец"])
        with self.assertRaises(ValueError):
            tasks.reorder(self.recordings, "потом", [custom.key])

    def test_filters(self):
        board = tasks.tasks(self.recordings)
        keys = {z.text: z.key for z in board}
        tasks.edit(
            self.recordings,
            keys["Проверить коэффициент коробки."],
            tasks.Edit(
                labels=("Склад",),
                priority="высокий",
                due=TODAY + datetime.timedelta(days=2),
                checklist=(tasks.ChecklistItem("Ёмкость коробки"),),
            ),
        )
        tasks.edit(self.recordings, keys["Как пересчитывать коробки."], tasks.Edit(archive=True))
        board = tasks.tasks(self.recordings)

        def texts(**fields: object) -> list[str]:
            return [z.text for z in tasks.pick_out(board, tasks.Filter(**fields), TODAY)]

        check = "Проверить коэффициент коробки."
        self.assertEqual(texts(), ["Доделать загрузку.", check])  # archive hidden
        self.assertEqual(texts(archive=True), ["Как пересчитывать коробки."])
        self.assertEqual(texts(who="Игорь"), ["Доделать загрузку."])
        self.assertEqual(texts(label="Склад"), [check])
        self.assertEqual(texts(priority=""), ["Доделать загрузку."])
        self.assertEqual(texts(priority="высокий"), [check])
        self.assertEqual(texts(due=tasks.OVERDUE), ["Доделать загрузку."])
        self.assertEqual(texts(due=tasks.THIS_WEEK), [check])  # up to Sunday
        self.assertEqual(texts(due=tasks.NO_DUE_DATE), [])
        self.assertEqual(texts(text="емкость"), [check])  # also in the checklist, ignoring "ё"
        self.assertEqual(texts(text="ОСТАТКОВ"), ["Доделать загрузку.", check])  # also in the source
        self.assertEqual(tasks.assignees(board), ["Дмитрий", "Игорь"])  # a question has no assignee

    def test_broken_cards_and_minutes_are_skipped(self):
        """One hand-edited card or minutes file in another encoding must not take the whole board down."""
        key = next(z.key for z in tasks.tasks(self.recordings) if z.text == "Доделать загрузку.")
        broken_meeting = self.recordings / "2026-10-15_10-00"
        broken_meeting.mkdir()
        transcript.save(
            broken_meeting,
            [{"начало": 1.0, "конец": 2.0, "кто": "Я", "текст": "Да."}],
            {"длительность": 60, "провайдер": "x", "стоимость": 0.1},
            [],
        )
        (broken_meeting / minutes.FILE).write_bytes(MINUTES.encode("cp1251"))
        (self.recordings / tasks.FILE).parent.mkdir(parents=True, exist_ok=True)
        (self.recordings / tasks.FILE).write_text(
            json.dumps(
                {
                    "правки": {key: {"чек_лист": [{"сделан": True}]}},
                    "метки": [{"цвет": 1}, {"имя": "Склад"}],
                    "свои": [
                        {"ид": "a1", "текст": "Срок по-русски", "срок": "16.10.2026", "создана": "2026-10-20"},
                        {"ид": "a2", "текст": "Целая", "создана": "2026-10-20"},
                        {"текст": "Без ключа", "создана": "2026-10-20"},
                        "не карточка",
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with self.assertLogs("knotes.tasks", "WARNING"):
            board = tasks.tasks(self.recordings)
        self.assertEqual(
            sorted(z.text for z in board), ["Как пересчитывать коробки.", "Проверить коэффициент коробки.", "Целая"]
        )
        with self.assertLogs("knotes.tasks", "WARNING"):
            self.assertEqual(tasks.labels(self.recordings), [tasks.Label("Склад", 0)])

    def test_meeting_list_already_read_is_not_read_again(self):
        meetings = catalog.list_(self.recordings)
        with mock.patch.object(catalog, "list_", side_effect=AssertionError("the folder is read again")):
            self.assertEqual(len(tasks.tasks(self.recordings, meetings=meetings)), 3)

    def test_edit_of_a_card_gone_from_rewritten_minutes_is_not_written(self):
        task = tasks.tasks(self.recordings)[0]
        (self.meeting / minutes.FILE).write_text(MINUTES.replace("Доделать загрузку", "Начать загрузку"), "utf-8")
        with self.assertRaises(tasks.GoneError):
            tasks.edit(self.recordings, task.key, tasks.Edit(description="Через склад"))
        self.assertFalse((self.recordings / tasks.FILE).exists())
        self.assertIsNone(tasks.refreshed(self.recordings, task))

    def test_card_refreshed_without_reading_the_board(self):
        task = tasks.tasks(self.recordings)[1]
        tasks.edit(self.recordings, task.key, tasks.Edit(description="Письмом", status=tasks.IN_PROGRESS))
        with mock.patch.object(catalog, "list_", side_effect=AssertionError("the whole board is read")):
            fresh = tasks.refreshed(self.recordings, task)
        self.assertEqual((fresh.description, fresh.status, fresh.origin), ("Письмом", tasks.IN_PROGRESS, task.origin))
        custom = tasks.add(self.recordings, "Своя", "Склад")
        tasks.edit(self.recordings, custom.key, tasks.Edit(who="Анна"))
        self.assertEqual(tasks.refreshed(self.recordings, custom).who, "Анна")
        tasks.delete(self.recordings, custom.key)
        self.assertIsNone(tasks.refreshed(self.recordings, custom))

    def test_order_counted_over_the_whole_column(self):
        """A filter hides some cards: dragging among the visible ones must not shuffle the hidden ones."""
        keys = ["a", "b", "c", "d"]
        self.assertEqual(tasks.placed(keys, "d", before="b"), ["a", "d", "b", "c"])
        self.assertEqual(tasks.placed(keys, "a", after="c"), ["b", "c", "a", "d"])
        self.assertEqual(tasks.placed(keys, "b"), ["a", "c", "d", "b"])
        self.assertEqual(tasks.placed(keys, "new", before="gone"), ["a", "b", "c", "d", "new"])

    def test_cards_that_keep_their_rows_when_one_moves(self):
        self.assertEqual(tasks.kept(list("abcde"), list("aebcd")), set("abcd"))  # "e" moved up
        self.assertEqual(tasks.kept(list("abcde"), list("bcdea")), set("bcde"))  # "a" moved down
        self.assertEqual(tasks.kept(list("abc"), list("xcab")), set("ab"))  # a new card, "c" moved up
        self.assertEqual(tasks.kept([], ["a"]), set())

    def test_service_folder_is_not_meeting(self):
        tasks.add(self.recordings, "Х")
        self.assertEqual([v.folder.name for v in catalog.list_(self.recordings)], ["2026-10-14_15-00"])


class TestColumns(TestBoard):
    """Own columns of a project board: each counts as one of the three bases, the card keeps its column."""

    def _columns(self, project: str = "Склад") -> list[tuple[str, str]]:
        return [(projects.column_name(c), c.base) for c in projects.columns(self.recordings, project)]

    def test_old_file_has_base_columns_and_default_is_not_stored(self):
        (self.recordings / projects.FILE).parent.mkdir(parents=True, exist_ok=True)
        (self.recordings / projects.FILE).write_text(
            json.dumps({"Склад": {"заметки": "ЗУП 3.1"}}, ensure_ascii=False), encoding="utf-8"
        )
        self.assertEqual(self._columns(), [("Сделать", "сделать"), ("В работе", "в работе"), ("Готово", "готово")])
        projects.set_columns(self.recordings, "Склад", list(projects.BASE_COLUMNS))
        self.assertNotIn("столбцы", json.loads((self.recordings / projects.FILE).read_text(encoding="utf-8"))["Склад"])
        self.assertEqual({z.column for z in tasks.tasks(self.recordings)}, {tasks.TODO})

    def test_add_rename_order_columns(self):
        review = projects.add_column(self.recordings, "Склад", "  На   проверке ", tasks.IN_PROGRESS)
        self.assertEqual((review.name, review.base), ("На проверке", tasks.IN_PROGRESS))
        for name in ("", "на проверке", "Готово"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                projects.add_column(self.recordings, "Склад", name, tasks.TODO)
        projects.rename_column(self.recordings, "Склад", tasks.TODO, "Очередь")
        projects.order_columns(self.recordings, "Склад", [tasks.TODO, review.key, tasks.IN_PROGRESS, tasks.DONE])
        self.assertEqual(
            self._columns(),
            [("Очередь", "сделать"), ("На проверке", "в работе"), ("В работе", "в работе"), ("Готово", "готово")],
        )
        self.assertEqual(self._columns("Отчеты")[0], ("Сделать", "сделать"))  # another project keeps its own
        stored = json.loads((self.recordings / projects.FILE).read_text(encoding="utf-8"))["Склад"]["столбцы"]
        self.assertEqual(stored[1], {"ключ": review.key, "основа": "в работе", "имя": "На проверке"})
        projects.rename(self.recordings, "Склад", "Склад 2")
        self.assertEqual(self._columns("Склад 2")[1], ("На проверке", "в работе"))
        with self.assertRaises(ValueError):
            projects.rename_column(self.recordings, "Склад 2", "нет", "Х")

    def test_columns_put_in_the_order_dragged(self):
        """The Columns window sends the whole new order, as the list stands after a drag."""
        review = projects.add_column(self.recordings, "Склад", "На проверке", tasks.IN_PROGRESS)
        projects.order_columns(self.recordings, "Склад", [review.key, tasks.TODO, tasks.IN_PROGRESS, tasks.DONE])
        self.assertEqual([c.key for c in projects.columns(self.recordings, "Склад")][:2], [review.key, tasks.TODO])
        for wrong in ([tasks.TODO, tasks.DONE], [tasks.TODO, tasks.TODO, tasks.IN_PROGRESS, review.key, tasks.DONE]):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):  # the board changed meanwhile
                projects.order_columns(self.recordings, "Склад", wrong)

    def test_card_keeps_its_column_and_counts_by_base(self):
        review = projects.add_column(self.recordings, "Склад", "На проверке", tasks.IN_PROGRESS)
        first, second = tasks.tasks(self.recordings)[:2]
        tasks.reorder(self.recordings, tasks.IN_PROGRESS, [first.key, second.key], review.key)
        board = {z.key: z for z in tasks.tasks(self.recordings)}
        self.assertEqual((board[first.key].column, board[first.key].status), (review.key, tasks.IN_PROGRESS))
        self.assertTrue(board[first.key].opened)
        self.assertEqual(self._file()["состояния"][first.key], tasks.IN_PROGRESS)  # older versions read the base
        tasks.edit(
            self.recordings, second.key, tasks.Edit(status=tasks.DONE)
        )  # the assistant sets a state, not a column
        tasks.edit(self.recordings, second.key, tasks.Edit(status=tasks.IN_PROGRESS))
        self.assertEqual(self._by_text()[second.text].column, tasks.IN_PROGRESS)  # the first column of the state
        projects.order_columns(self.recordings, "Склад", [tasks.TODO, review.key, tasks.IN_PROGRESS, tasks.DONE])
        self.assertEqual(self._by_text()[first.text].column, review.key)  # the stored column stays
        self.assertEqual(self._by_text()[second.text].column, review.key)  # now the first column of the state
        custom = tasks.add(
            self.recordings, "Своя", "Склад", tasks.Edit(status=tasks.IN_PROGRESS, column=review.key, order=3)
        )
        self.assertEqual((custom.column, custom.order), (review.key, 3))
        (self.recordings / projects.FILE).write_text("{}", encoding="utf-8")  # columns gone by hand
        self.assertEqual(
            {z.column for z in tasks.tasks(self.recordings) if z.status == tasks.IN_PROGRESS}, {"в работе"}
        )

    def test_delete_column_moves_cards(self):
        review = projects.add_column(self.recordings, "Склад", "На проверке", tasks.IN_PROGRESS)
        first = tasks.tasks(self.recordings)[0]
        tasks.reorder(self.recordings, tasks.IN_PROGRESS, [first.key], review.key)
        with self.assertRaises(ValueError):
            tasks.delete_column(self.recordings, "Склад", tasks.DONE, tasks.TODO)  # the only "done" column
        with self.assertRaises(ValueError):
            tasks.delete_column(self.recordings, "Склад", review.key, review.key)
        tasks.delete_column(self.recordings, "Склад", review.key, tasks.DONE)
        moved = self._by_text()[first.text]
        self.assertEqual((moved.column, moved.status, moved.order), (tasks.DONE, tasks.DONE, None))
        self.assertEqual(len(projects.columns(self.recordings, "Склад")), 3)
        self.assertNotIn("Склад", json.loads((self.recordings / projects.FILE).read_text(encoding="utf-8")))

    def test_rebase_column_changes_card_states(self):
        waiting = projects.add_column(self.recordings, "Склад", "Ждет клиента", tasks.TODO)
        first, second = tasks.tasks(self.recordings)[:2]
        tasks.reorder(self.recordings, tasks.TODO, [second.key, first.key], waiting.key)
        tasks.rebase_column(self.recordings, "Склад", waiting.key, tasks.IN_PROGRESS)
        board = self._by_text()
        self.assertEqual(
            [(board[z.text].status, board[z.text].column, board[z.text].order) for z in (second, first)],
            [(tasks.IN_PROGRESS, waiting.key, 0), (tasks.IN_PROGRESS, waiting.key, 1)],
        )
        tasks.rebase_column(self.recordings, "Склад", waiting.key, tasks.IN_PROGRESS)  # the same base: nothing
        with self.assertRaises(ValueError):
            tasks.rebase_column(self.recordings, "Склад", tasks.TODO, tasks.DONE)  # the only "to do" column
        with self.assertRaises(ValueError):
            tasks.rebase_column(self.recordings, "Склад", waiting.key, "потом")

    def test_marks_on_the_board_of_all_projects(self):
        review = projects.add_column(self.recordings, "Склад", "На проверке", tasks.IN_PROGRESS)
        first, second, third = tasks.tasks(self.recordings)
        tasks.reorder(self.recordings, tasks.IN_PROGRESS, [first.key], review.key)
        tasks.edit(self.recordings, second.key, tasks.Edit(status=tasks.IN_PROGRESS))
        projects.rename_column(self.recordings, "Склад", tasks.TODO, "Очередь")
        self.assertEqual(
            tasks.column_marks(self.recordings, tasks.tasks(self.recordings)),
            {first.key: "На проверке", third.key: "Очередь"},
        )

    def test_broken_columns_in_file(self):
        (self.recordings / projects.FILE).parent.mkdir(parents=True, exist_ok=True)
        broken = [{"ключ": "x", "основа": "потом"}, {"основа": "готово"}, ["список"], {"ключ": "y", "основа": "готово"}]
        (self.recordings / projects.FILE).write_text(
            json.dumps({"Склад": {"столбцы": broken}, "Отчеты": "не словарь"}, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertLogs("knotes.projects", "WARNING"):
            self.assertEqual([c.key for c in projects.columns(self.recordings, "Склад")], ["y", "сделать", "в работе"])
        self.assertEqual(len(projects.columns(self.recordings, "Отчеты")), 3)
        projects.set_kind(self.recordings, "Отчеты", "Демо")  # a broken project entry is written anew
        self.assertEqual(projects.kind(self.recordings, "Отчеты"), "Демо")


class TestProjectNotes(unittest.TestCase):
    def test_project_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            recordings = pathlib.Path(tmp)
            self.assertEqual(projects.notes(recordings, "Склад"), projects.ProjectNotes())
            projects.set_(recordings, "Склад", projects.ProjectNotes("У клиента ЗУП 3.1.", for_model=False))
            self.assertEqual(projects.notes(recordings, "Склад"), projects.ProjectNotes("У клиента ЗУП 3.1.", False))
            projects.rename(recordings, "Склад", "Склад 2")
            self.assertEqual(projects.notes(recordings, "Склад 2").text, "У клиента ЗУП 3.1.")
            self.assertEqual(projects.notes(recordings, "Склад").text, "")
            projects.rename(recordings, "Склад 2", "")  # project deleted: no notes
            self.assertEqual(projects.notes(recordings, "Склад 2").text, "")
            projects.set_(recordings, "Отчеты", projects.ProjectNotes("  "))
            self.assertFalse((recordings / projects.FILE).read_text(encoding="utf-8").count("Отчеты"))


if __name__ == "__main__":
    unittest.main()
