# SPDX-License-Identifier: GPL-3.0-only
"""The "Tasks" page: columns by status and project columns, moving and reordering by dragging, filters, the card
panel with editing and autosave, quick add, the card menu, keys, archive, failed writes, plain text in messages."""

import datetime
import errno
import pathlib
import tempfile
import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QLabel, QMenu, QToolButton

from knotes import tasks, catalog, minutes, projects, transcript
from knotes.ui import board_panel, style, tasks_window
from knotes.ui.board_columns import TASK_MIME
from knotes.ui.board_style import OPENED
from knotes.ui.tasks_window import TasksWindow

app = QApplication.instance() or QApplication([])
MINUTES = (
    "# Итоги\n\n## Задачи\n\n1. Доделать загрузку. Игорь, 16.10.2020. [09:10]\n2. Проверить коэффициент. "
    "Дмитрий. [09:30]\n\n## Открытые вопросы\n\n1. Как пересчитывать коробки. Анна уточнит. [09:18]\n"
)
ROLE = Qt.ItemDataRole.UserRole


def _item(menu: QMenu, *path: str):
    """A menu item by its caption path, e.g. ("Priority", "High")."""
    for row_label in path[:-1]:
        menu = next(d.menu() for d in menu.actions() if d.text() == row_label)
    return next(d for d in menu.actions() if d.text() == path[-1])


class BoardCase(unittest.TestCase):
    """A meeting of the "Склад" project with tasks and a question in its minutes, the board of that project open."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.recordings = pathlib.Path(self._tmp.name)
        folder = self.recordings / "2026-10-14_15-00"
        folder.mkdir()
        transcript.save(
            folder,
            [{"начало": 5.0, "конец": 9.0, "кто": "Я", "текст": "Смотрим."}],
            {"длительность": 600, "провайдер": "x", "стоимость": 0.1},
            [],
        )
        (folder / minutes.FILE).write_text(MINUTES, encoding="utf-8")
        catalog.set_(folder, title="Загрузка остатков", project="Склад")
        self.folder = folder
        self.window = self._window()

    def _window(self) -> TasksWindow:
        window = TasksWindow(self.recordings, (["Склад"], {}), "Склад")
        window.resize(1300, 700)
        window.show()
        app.processEvents()
        return window

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        app.processEvents()
        self._tmp.cleanup()

    def _texts(self, status: str) -> list[str]:
        col = self.window.columns[status]
        return [col.item(n).data(ROLE).text for n in range(col.count()) if not col.item(n).isHidden()]

    def _task(self, text: str) -> tasks.Task:
        return next(z for z in tasks.tasks(self.recordings) if z.text == text)

    def _card(self, status: str, number: int = 0):
        col = self.window.columns[status]
        return col.itemWidget([col.item(n) for n in range(col.count()) if not col.item(n).isHidden()][number])


class TestTasksWindow(BoardCase):
    def test_columns_overdue_on_top_and_source(self):
        self.assertEqual(
            self._texts(tasks.TODO), ["Доделать загрузку.", "Проверить коэффициент.", "Как пересчитывать коробки."]
        )
        card = self._card(tasks.TODO)
        self.assertTrue(any(m.objectName() == "ошибка_текст" for m in card.findChildren(QLabel)))  # overdue
        texts = [m.text() for m in card.findChildren(QLabel)]
        self.assertTrue(any("Загрузка остатков, 14.10.2026, 09:10" in tr for tr in texts))
        places: list[tuple] = []
        self.window.open_place.connect(lambda p, s: places.append((p, s)))
        next(m for m in card.findChildren(QLabel) if "Основание" in m.text()).linkActivated.emit("#")
        self.assertEqual(places, [(self.folder, 550.0)])
        self.assertEqual(self.window._counters[tasks.TODO].text(), "3")
        question = self._card(tasks.TODO, 2)
        self.assertEqual(question.objectName(), "вопрос_доски")  # dashed border
        self.assertTrue(any(m.text().startswith("вопрос · Анна") for m in question.findChildren(QLabel)))
        self.assertFalse(hasattr(self.window, "notes"))  # project notes are not on the board

    def test_move_to_column_and_position(self):
        col = self.window.columns[tasks.TODO]
        key = col.item(0).data(ROLE).key
        data = col.mimeData([col.item(0)])
        self.assertEqual(bytes(data.data(TASK_MIME)).decode(), key)  # the task key is dragged
        self.window.columns[tasks.DONE].moved_item.emit(key, tasks.DONE, 0)
        app.processEvents()
        self.assertEqual(self._texts(tasks.DONE), ["Доделать загрузку."])
        question = col.item(1).data(ROLE).key
        col.moved_item.emit(question, tasks.TODO, 0)  # higher in the same column
        app.processEvents()
        self.assertEqual(self._texts(tasks.TODO), ["Как пересчитывать коробки.", "Проверить коэффициент."])
        col.moved_item.emit(question, tasks.TODO, 2)  # to the very bottom
        app.processEvents()
        self.assertEqual(self._texts(tasks.TODO), ["Проверить коэффициент.", "Как пересчитывать коробки."])
        self.window.close()
        self.window.deleteLater()
        self.window = self._window()  # the order is remembered
        self.assertEqual(self._texts(tasks.TODO), ["Проверить коэффициент.", "Как пересчитывать коробки."])

    def test_search_lights_up_the_found_words_in_the_cards(self):
        """As written in the card, any case, "е" and "ё" alike; the card text stays plain text."""
        tasks.add(self.recordings, "Сверить <b>коробки</b> на складе", "Склад")
        self.window.display()
        self.window.search("КОРОБКИ")
        QTest.qWait(400)
        titles = {self._card(tasks.TODO, n).title.text() for n in range(2)}
        lit = f"<span style='background:{style.HIGHLIGHT[style.dark()][0]}'>коробки</span>"
        self.assertIn(f"Как пересчитывать {lit}.", titles)
        self.assertIn(f"Сверить &lt;b&gt;{lit}&lt;/b&gt; на складе", titles)
        self.window.search("")
        QTest.qWait(400)
        title = self._card(tasks.TODO).title
        self.assertEqual((title.textFormat(), "span" in title.text()), (Qt.TextFormat.PlainText, False))

    def test_filters_and_counters(self):
        tasks.edit(
            self.recordings, self._task("Проверить коэффициент.").key, tasks.Edit(labels=("Склад",), priority="высокий")
        )
        self.window.display()
        filters = self.window.filters
        filters["кто"].select("Дмитрий")
        self.assertEqual(self._texts(tasks.TODO), ["Проверить коэффициент."])
        self.assertEqual(filters["кто"].text(), "Дмитрий")
        filters["кто"].select(None)
        filters["метка"].select("Склад")
        self.assertEqual(self.window._counters[tasks.TODO].text(), "1")
        filters["метка"].select(None)
        filters["приоритет"].select("")
        self.assertEqual(self._texts(tasks.TODO), ["Доделать загрузку.", "Как пересчитывать коробки."])
        filters["приоритет"].select(None)
        filters["срок"].select(tasks.OVERDUE)
        self.assertEqual(self._texts(tasks.TODO), ["Доделать загрузку."])
        filters["срок"].select(None)
        self.window.search("коробки")
        QTest.qWait(400)
        self.assertEqual(self._texts(tasks.TODO), ["Как пересчитывать коробки."])
        self.window.search("")
        QTest.qWait(400)
        tasks.edit(self.recordings, self._task("Доделать загрузку.").key, tasks.Edit(archive=True))
        self.window.display()
        self.assertEqual(self.window._counters[tasks.TODO].text(), "2")  # the archived one is hidden
        self.window.archive.setChecked(True)
        self.assertEqual(self._texts(tasks.TODO), ["Доделать загрузку."])
        self.assertTrue(self.window.adding[tasks.TODO].isHidden())  # nothing is added to the archive

    def test_panel_edits_and_saves_itself(self):
        col = self.window.columns[tasks.TODO]
        col.itemClicked.emit(col.item(1))  # "Проверить коэффициент."
        app.processEvents()
        panel = self.window.panel
        self.assertFalse(panel.isHidden())
        self.assertTrue(self._card(tasks.TODO, 1).property(OPENED))
        panel.heading.setPlainText("Проверить коэффициент коробки")
        panel.description.setPlainText("По весу и объему")
        panel.assignee.setCurrentText("Анна")
        panel.priority.setCurrentIndex(panel.priority.findData("срочный"))
        panel.set_due(datetime.date(2030, 1, 15))
        panel.toggle_label("Склад")
        panel.checklist.add("Спросить склад")
        panel.checklist.add("Пересчитать")
        panel.checklist.mark(0, True)
        QTest.qWait(700)  # saves by itself
        task = self._task("Проверить коэффициент коробки")
        self.assertEqual(
            (task.description, task.who, task.priority, task.due, task.labels),
            ("По весу и объему", "Анна", "срочный", datetime.date(2030, 1, 15), ("Склад",)),
        )
        self.assertEqual(
            task.checklist, (tasks.ChecklistItem("Спросить склад", True), tasks.ChecklistItem("Пересчитать"))
        )
        self.assertEqual((self.folder / minutes.FILE).read_text(encoding="utf-8"), MINUTES)
        self.assertIn("Проверить коэффициент коробки", self._texts(tasks.TODO))
        self.assertEqual([m.name for m in tasks.labels(self.recordings)], ["Склад"])
        panel.checklist.remove_item(1)
        panel.description.setPlainText("По весу")
        QTest.keyClick(self.window, Qt.Key.Key_Escape)  # closing saves the unfinished text too
        app.processEvents()
        self.assertTrue(panel.isHidden())
        task = self._task("Проверить коэффициент коробки")
        self.assertEqual((task.description, len(task.checklist)), ("По весу", 1))
        col = self.window.columns[tasks.TODO]
        self.window.open_task(col.item(0).data(ROLE))
        panel.status.setCurrentIndex(panel.status.findData(tasks.IN_PROGRESS))  # moves to another column at once
        app.processEvents()
        self.assertEqual(len(self._texts(tasks.IN_PROGRESS)), 1)

    def test_quick_add_and_new_task(self):
        field = self.window.adding[tasks.IN_PROGRESS]
        field.setText("Позвонить складу")
        field.returnPressed.emit()
        app.processEvents()
        self.assertEqual(self._texts(tasks.IN_PROGRESS), ["Позвонить складу"])
        self.assertEqual((field.text(), self._task("Позвонить складу").project), ("", "Склад"))
        field.setText("Второй звонок")
        field.returnPressed.emit()
        app.processEvents()
        self.assertEqual(self._texts(tasks.IN_PROGRESS), ["Позвонить складу", "Второй звонок"])  # at the bottom
        self.window.new_task()
        panel = self.window.panel
        self.assertFalse(panel.isHidden())
        self.assertIsNone(panel.task)
        panel.close_view()  # without a text there is nothing to create
        self.assertEqual(len(tasks.tasks(self.recordings)), 5)
        self.window.new_task()
        panel.heading.setPlainText("Согласовать выгрузку")
        panel.set_due(datetime.date(2030, 1, 1))
        panel.close_view()
        app.processEvents()
        custom = self._task("Согласовать выгрузку")
        self.assertEqual((custom.due, custom.project, custom.status), (datetime.date(2030, 1, 1), "Склад", tasks.TODO))
        self.assertIn("Согласовать выгрузку", self._texts(tasks.TODO))

    def test_card_menu(self):
        finish = self._task("Доделать загрузку.")
        menu = self.window._task_menu(finish)
        row_labels = [d.text() for d in menu.actions()]
        for row_label in (
            "Состояние",
            "Приоритет",
            "Метки",
            "Срок",
            "Исполнитель",
            "Открыть встречу на этом месте",
            "В архив",
        ):
            self.assertIn(row_label, row_labels)
        self.assertNotIn("Удалить…", row_labels)  # a minutes item is not deleted
        _item(menu, "Приоритет", "Высокий").trigger()
        app.processEvents()
        _item(self.window._task_menu(finish), "Срок", "Завтра").trigger()
        app.processEvents()
        _item(self.window._task_menu(finish), "Исполнитель", "Дмитрий").trigger()
        app.processEvents()
        with mock.patch.object(QInputDialog, "getText", return_value=("Срочно", True)):
            _item(self.window._task_menu(finish), "Метки", "Новая метка…").trigger()
        app.processEvents()
        _item(self.window._task_menu(finish), "Состояние", "В работе").trigger()
        app.processEvents()
        finish = self._task("Доделать загрузку.")
        self.assertEqual(
            (finish.priority, finish.due, finish.who, finish.labels, finish.status),
            ("высокий", datetime.date.today() + datetime.timedelta(days=1), "Дмитрий", ("Срочно",), tasks.IN_PROGRESS),
        )
        self.assertEqual(self._texts(tasks.IN_PROGRESS), ["Доделать загрузку."])
        _item(self.window._task_menu(finish), "Срок", "Убрать срок").trigger()
        _item(self.window._task_menu(finish), "В архив").trigger()
        app.processEvents()
        self.assertEqual(self._texts(tasks.IN_PROGRESS), [])
        self.assertEqual((self._task("Доделать загрузку.").due, self._task("Доделать загрузку.").archive), (None, True))
        custom = tasks.add(self.recordings, "Своя", "Склад")
        self.window.display()
        with mock.patch.object(style, "ask", return_value=True):
            _item(self.window._task_menu(custom), "Удалить…").trigger()
        app.processEvents()
        self.assertNotIn("Своя", [z.text for z in tasks.tasks(self.recordings)])

    def test_keys_enter_opens_delete_archives(self):
        col = self.window.columns[tasks.TODO]
        col.setFocus()
        col.setCurrentRow(1)
        QTest.keyClick(col, Qt.Key.Key_Return)
        app.processEvents()
        self.assertEqual(self.window.panel.task.text, "Проверить коэффициент.")
        field = self.window.adding[tasks.TODO]
        field.setText("Недописанная")
        QTest.keyClick(field, Qt.Key.Key_Escape)  # Esc first clears the line, the panel stays
        self.assertEqual((field.text(), self.window.panel.isHidden()), ("", False))
        QTest.keyClick(col, Qt.Key.Key_Escape)  # then it closes the panel
        self.assertTrue(self.window.panel.isHidden())
        QTest.keyClick(col, Qt.Key.Key_Delete)
        app.processEvents()
        self.assertTrue(self._task("Проверить коэффициент.").archive)
        self.assertNotIn("Проверить коэффициент.", self._texts(tasks.TODO))

    def test_archive_and_delete_from_panel(self):
        col = self.window.columns[tasks.TODO]
        self.window.open_task(col.item(0).data(ROLE))
        panel = self.window.panel
        self.assertTrue(panel.delete.isHidden())  # a minutes item is not deleted
        panel.archive.click()
        app.processEvents()
        self.assertTrue(self._task("Доделать загрузку.").archive)
        self.assertNotIn("Доделать загрузку.", self._texts(tasks.TODO))
        panel.archive.click()  # restore
        app.processEvents()
        self.assertIn("Доделать загрузку.", self._texts(tasks.TODO))
        custom = tasks.add(self.recordings, "Своя", "Склад")
        self.window.display()
        self.window.open_task(custom)
        with mock.patch.object(style, "ask", return_value=True):
            panel.delete.click()
        app.processEvents()
        self.assertTrue(panel.isHidden())
        self.assertNotIn("Своя", self._texts(tasks.TODO))


class TestBoardFailures(BoardCase):
    """A write that fails keeps what the user typed and says why; a card gone from the board closes its panel."""

    def test_quick_add_keeps_text_when_not_written(self):
        field = self.window.adding[tasks.TODO]
        field.setText("Позвонить складу")
        full = OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(tasks, "add", side_effect=full), mock.patch.object(style, "ask") as message:
            field.returnPressed.emit()
        self.assertEqual(field.text(), "Позвонить складу")
        self.assertIn("закончилось место", message.call_args.args[2])
        field.returnPressed.emit()
        app.processEvents()
        self.assertEqual(field.text(), "")
        self.assertIn("Позвонить складу", self._texts(tasks.TODO))

    def test_panel_closes_after_a_failed_save_only_on_second_try(self):
        col = self.window.columns[tasks.TODO]
        self.window.open_task(col.item(0).data(ROLE))
        panel = self.window.panel
        panel.description.setPlainText("Через склад")
        denied = PermissionError(errno.EACCES, "Access is denied", "задачи.json")
        with mock.patch.object(tasks, "edit", side_effect=denied), mock.patch.object(style, "ask") as message:
            QTest.keyClick(self.window, Qt.Key.Key_Escape)
            self.assertFalse(panel.isHidden())  # the text can still be copied
            self.assertEqual(panel.description.toPlainText(), "Через склад")
            QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertTrue(panel.isHidden())
        self.assertTrue(all("задачи.json" in c.args[2] for c in message.call_args_list))
        self.assertEqual(self._task("Доделать загрузку.").description, "")

    def test_checklist_item_not_doubled_after_failure(self):
        col = self.window.columns[tasks.TODO]
        self.window.open_task(col.item(0).data(ROLE))
        checklist = self.window.panel.checklist
        checklist.new.setText("Спросить склад")
        with mock.patch.object(tasks, "edit", side_effect=OSError("disk")), mock.patch.object(style, "ask"):
            checklist.new.returnPressed.emit()
        self.assertEqual((checklist.items(), checklist.new.text()), ((), "Спросить склад"))
        checklist.new.returnPressed.emit()
        self.assertEqual(checklist.new.text(), "")
        self.assertEqual(self._task("Доделать загрузку.").checklist, (tasks.ChecklistItem("Спросить склад"),))

    def test_unreadable_folder_gives_empty_board_with_reason(self):
        with (
            mock.patch.object(catalog, "list_", side_effect=PermissionError(13, "Access is denied", "Записи")),
            mock.patch.object(style, "ask") as message,
        ):
            self.window.display()
        self.assertEqual(self._texts(tasks.TODO), [])
        self.assertIn("Записи", message.call_args.args[2])

    def test_card_gone_from_rewritten_minutes_closes_panel(self):
        col = self.window.columns[tasks.TODO]
        self.window.open_task(col.item(0).data(ROLE))
        panel = self.window.panel
        (self.folder / minutes.FILE).write_text(MINUTES.replace("Доделать загрузку", "Начать загрузку"), "utf-8")
        panel.description.setPlainText("В никуда")
        with mock.patch.object(style, "ask") as message:
            panel.save()
            app.processEvents()
        self.assertTrue(panel.isHidden())
        self.assertIn("Доделать загрузку.", message.call_args.args[2])
        self.assertNotIn("правки", tasks._data(self.recordings))  # noqa: SLF001
        self.window.open_task(self.window.columns[tasks.TODO].item(0).data(ROLE))
        (self.folder / minutes.FILE).write_text("# Итоги\n", "utf-8")
        with mock.patch.object(style, "ask") as message:
            self.window.display()  # the usual refresh finds the card gone
        self.assertTrue(panel.isHidden())
        message.assert_called_once()


class TestBoardSpeed(BoardCase):
    def test_cards_do_not_read_meetings_and_panel_redraws_one_card(self):
        catalog.set_(self.folder, title="")  # the default title, shown in the interface language
        self.window.display()
        with (
            mock.patch.object(catalog, "display_title", side_effect=AssertionError("a card read its meeting")),
            mock.patch.object(catalog, "properties", side_effect=AssertionError("a card read its meeting")),
        ):
            self.window.display(reread=False)
        texts = [m.text() for m in self._card(tasks.TODO).findChildren(QLabel)]
        self.assertTrue(any("Встреча 14.10" in tr for tr in texts))
        other = self._card(tasks.TODO, 2)
        self.window.open_task(self.window.columns[tasks.TODO].item(0).data(ROLE))
        self.window.panel.description.setPlainText("Через склад")
        with mock.patch.object(tasks, "tasks", side_effect=AssertionError("the whole board is read")):
            QTest.qWait(700)
        self.assertIs(self._card(tasks.TODO, 2), other)  # the other cards stay as they were
        self.assertEqual(self.window.columns[tasks.TODO].item(0).data(ROLE).description, "Через склад")
        self.window.columns[tasks.TODO].moved_item.emit(self._task("Доделать загрузку.").key, tasks.TODO, 3)
        app.processEvents()
        self.assertEqual(self._texts(tasks.TODO)[-1], "Доделать загрузку.")
        self.assertIs(self._card(tasks.TODO, 1), other)  # dragging redraws only the dragged card

    def test_done_column_shows_first_cards_and_more(self):
        for number in range(tasks_window.SHOWN + 5):
            tasks.add(self.recordings, f"Сделано {number}", "Склад", tasks.Edit(status=tasks.DONE, order=number))
        self.window.display()
        more = self.window._more[tasks.DONE]  # noqa: SLF001
        self.assertEqual(len(self._texts(tasks.DONE)), tasks_window.SHOWN)
        self.assertEqual(self.window._counters[tasks.DONE].text(), str(tasks_window.SHOWN + 5))  # noqa: SLF001
        self.assertFalse(more.isHidden())
        self.assertIn("5", more.text())
        more.click()
        self.assertEqual(len(self._texts(tasks.DONE)), tasks_window.SHOWN + 5)
        self.assertTrue(more.isHidden())

    def test_drag_with_filter_keeps_order_of_hidden_cards(self):
        for order, (text, who) in enumerate((("Первая", "Анна"), ("Вторая", "Борис"), ("Третья", "Анна"))):
            tasks.add(self.recordings, text, "Склад", tasks.Edit(who=who, status=tasks.IN_PROGRESS, order=order))
        tasks.add(self.recordings, "Четвертая", "Склад", tasks.Edit(who="Борис", status=tasks.IN_PROGRESS, order=3))
        self.window.display()
        self.window.filters["кто"].select("Анна")
        self.assertEqual(self._texts(tasks.IN_PROGRESS), ["Первая", "Третья"])
        col = self.window.columns[tasks.IN_PROGRESS]
        col.moved_item.emit(self._task("Первая").key, tasks.IN_PROGRESS, 2)  # below "Третья"
        app.processEvents()
        self.assertEqual(self._texts(tasks.IN_PROGRESS), ["Третья", "Первая"])
        self.window.filters["кто"].select(None)
        self.assertEqual(self._texts(tasks.IN_PROGRESS), ["Вторая", "Третья", "Первая", "Четвертая"])


class TestPlainText(BoardCase):
    def test_message_text_is_plain(self):
        boxes = []

        def shown(box):
            boxes.append((box.textFormat(), box.text()))
            return style.QMessageBox.StandardButton.Yes

        with mock.patch.object(style.QMessageBox, "exec", shown):
            self.assertTrue(style.ask(self.window, "Вопрос", '<a href="file:///C:/x.exe">Открыть</a>'))
        self.assertEqual(boxes, [(Qt.TextFormat.PlainText, '<a href="file:///C:/x.exe">Открыть</a>')])

    def test_task_with_link_asked_as_text(self):
        custom = tasks.add(self.recordings, '<a href="file:///C:/x.exe">Открыть</a>', "Склад")
        self.window.display()
        with mock.patch.object(style, "ask", return_value=False) as question:
            _item(self.window._task_menu(custom), "Удалить…").trigger()
        self.assertIn('<a href="file:///C:/x.exe">', question.call_args.args[2])

    def test_ampersand_in_labels_and_names_is_shown(self):
        tasks.edit(self.recordings, self._task("Доделать загрузку.").key, tasks.Edit(labels=("R&D",), who="A&B"))
        self.window.display()
        self.window.filters["метка"].select("R&D")
        self.assertEqual(self.window.filters["метка"].text(), "R&&D")
        menu = self.window._task_menu(self._task("Доделать загрузку."))
        self.assertIn("R&&D", [d.text() for d in _item(menu, "Метки", "R&&D").parent().actions()])
        self.assertTrue(_item(menu, "Исполнитель", "A&&B").isChecked())


class TestProjectColumns(BoardCase):
    def test_header_has_no_menu_and_a_double_click_renames_in_place(self):
        """The header is the category sign, the name and the count; the name is edited where it is: Enter saves,
        Esc leaves it as it was."""
        title = self.window._titles[tasks.TODO]
        header = title.parentWidget()
        self.assertEqual(header.findChildren(QToolButton), [])
        self.assertEqual(header.contextMenuPolicy(), Qt.ContextMenuPolicy.DefaultContextMenu)
        QTest.mouseDClick(title.label, Qt.MouseButton.LeftButton)
        self.assertTrue(title.editing())
        title.field.setText("Очередь")
        QTest.keyClick(title.field, Qt.Key.Key_Return)
        app.processEvents()
        self.assertEqual(projects.column_name(projects.columns(self.recordings, "Склад")[0]), "Очередь")
        title = self.window._titles[tasks.TODO]  # the board was built again
        self.assertEqual(title.label.full(), "Очередь")
        QTest.mouseDClick(title.label, Qt.MouseButton.LeftButton)
        title.field.setText("Другое")
        QTest.keyClick(title.field, Qt.Key.Key_Escape)
        app.processEvents()
        self.assertFalse(title.editing())
        self.assertEqual(projects.column_name(projects.columns(self.recordings, "Склад")[0]), "Очередь")
        QTest.mouseDClick(title.label, Qt.MouseButton.LeftButton)
        title.field.setText("готово")
        with mock.patch.object(style, "ask") as refusal:
            QTest.keyClick(title.field, Qt.Key.Key_Return)
        self.assertIn("уже есть", refusal.call_args.args[2])
        self.assertEqual(title.label.full(), "Очередь")

    def test_board_follows_the_board_settings_and_all_projects_keep_base_names(self):
        dialog = self.window.board_settings()
        dialog.columns._add()
        app.processEvents()
        self.assertEqual(len(self.window.columns), 4)
        dialog.deleteLater()
        self.window._pick_project(None)
        title = self.window._titles[tasks.TODO]
        QTest.mouseDClick(title.label, Qt.MouseButton.LeftButton)
        self.assertFalse(title.editing())  # the base columns of all projects are not renamed

    def test_search_is_in_the_top_bar_and_its_hint_follows_the_project(self):
        hints: list[str] = []
        self.window.search_hint_changed.connect(hints.append)
        self.assertEqual(self.window.search_hint, "Поиск по задачам проекта Склад  Ctrl+F")
        self.window._pick_project(None)
        self.window._pick_project("")
        self.assertEqual(hints, ["Поиск по всем задачам  Ctrl+F", "Поиск по задачам без проекта  Ctrl+F"])
        self.assertEqual(self.window.findChildren(style.ClearableLine)[0].objectName(), "быстрая")  # no own search

    def test_card_menu_and_panel_offer_project_columns(self):
        review = projects.add_column(self.recordings, "Склад", "На проверке", tasks.IN_PROGRESS)
        self.window.close()
        self.window = self._window()
        finish = self._task("Доделать загрузку.")
        _item(self.window._task_menu(finish), "Состояние", "На проверке").trigger()
        app.processEvents()
        self.assertEqual(self._texts(review.key), ["Доделать загрузку."])
        panel = self.window.panel
        self.window.open_task(self.window.columns[review.key].item(0).data(ROLE))
        self.assertEqual(panel.status.currentData(), review.key)
        panel.status.setCurrentIndex(panel.status.findData(tasks.DONE))
        app.processEvents()
        self.assertEqual(
            (self._task("Доделать загрузку.").status, self._task("Доделать загрузку.").column), (tasks.DONE, tasks.DONE)
        )

    def test_board_of_all_projects_has_base_columns_with_marks(self):
        review = projects.add_column(self.recordings, "Склад", "На проверке", tasks.IN_PROGRESS)
        tasks.reorder(self.recordings, tasks.IN_PROGRESS, [self._task("Доделать загрузку.").key], review.key)
        self.window._pick_project(None)
        self.assertEqual(list(self.window.columns), [tasks.TODO, tasks.IN_PROGRESS, tasks.DONE])
        self.assertEqual(self._texts(tasks.IN_PROGRESS), ["Доделать загрузку."])
        texts = [m.full() for m in self._card(tasks.IN_PROGRESS).findChildren(style.Elided)]
        self.assertIn("На проверке", texts)
        self.window.columns[tasks.DONE].moved_item.emit(self._task("Доделать загрузку.").key, tasks.DONE, 0)
        app.processEvents()
        self.assertEqual(self._task("Доделать загрузку.").column, tasks.DONE)


class TestChecklist(unittest.TestCase):
    def test_cross_removes_own_item(self):
        """The button passes "checked" to the handler: the cross of the third item must not remove the first one."""
        list_ = board_panel.Checklist()
        list_.set_((tasks.ChecklistItem("первый"), tasks.ChecklistItem("второй"), tasks.ChecklistItem("третий")))
        close_buttons = [k for k in list_.findChildren(QToolButton) if k.objectName() == "крестик"]
        close_buttons[2].click()
        self.assertEqual([p.text for p in list_.items()], ["первый", "второй"])
        list_.deleteLater()
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
