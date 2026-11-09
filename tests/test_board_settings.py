# SPDX-License-Identifier: GPL-3.0-only
"""The board settings window: its button on every board, the Columns tab (the project to set up, order by dragging,
the name in place, the category, a new column, delete with a place for the cards, the last column of a category
stays) and the Labels tab (the name in place, the color, a new label, delete); Enter in a name only finishes it;
every change reaches the board at once."""

import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QLabel, QToolButton, QWidget

from knotes import projects, tasks
from knotes.ui import board_columns_page, style
from knotes.ui.board_columns_page import KEY
from knotes.ui.board_settings import COLUMNS, LABELS, BoardSettings
from tests.test_tasks_window import BoardCase, _item, app


class SettingsCase(BoardCase):
    def setUp(self):
        super().setUp()
        self.review = projects.add_column(self.recordings, "Склад", "На проверке", tasks.IN_PROGRESS)
        self.dialog = self._dialog()
        self.page = self.dialog.columns

    def _dialog(self, tab: str = COLUMNS) -> BoardSettings:
        dialog = self.window.board_settings(tab)
        dialog.show()
        app.processEvents()
        return dialog

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        super().tearDown()

    def _keys(self) -> list[str]:
        return [c.key for c in projects.columns(self.recordings, "Склад")]

    def _row_buttons(self, key: str) -> list[QToolButton]:
        """The category button and the trash of the column's row."""
        element = next(
            self.page.list_.item(n) for n in range(self.page.list_.count()) if self.page.list_.item(n).data(KEY) == key
        )
        return self.page.list_.itemWidget(element).findChildren(QToolButton)


class TestSettingsButton(SettingsCase):
    def test_on_every_board_and_opens_the_board_project_or_the_first_one(self):
        self.assertEqual(self.window.settings_button.text(), "Настроить доску")
        self.assertFalse(self.window.settings_button.isHidden())
        shown: list[BoardSettings] = []
        with mock.patch.object(BoardSettings, "exec", lambda dialog: shown.append(dialog)):
            self.window.settings_button.click()
        self.assertEqual(shown[0].columns.project, "Склад")
        self.window._pick_project(None)
        self.assertFalse(self.window.settings_button.isHidden())  # the board of all projects has it too
        dialog = self._dialog()
        self.assertEqual((dialog.pages.currentWidget(), dialog.columns.project), (dialog.columns, "Склад"))
        dialog.close()
        dialog.deleteLater()

    def test_project_chosen_in_the_window_shows_its_columns(self):
        choice = self.page.choice
        self.assertEqual([choice.itemText(n) for n in range(choice.count())], ["Склад", "Без проекта"])
        choice.setCurrentIndex(1)
        self.assertEqual(self.page.project, "")
        self.assertEqual(sorted(self.page.names), sorted([tasks.TODO, tasks.IN_PROGRESS, tasks.DONE]))
        self.page._add()
        self.assertEqual(len(projects.columns(self.recordings, "")), 4)
        self.assertEqual(len(projects.columns(self.recordings, "Склад")), 4)  # the other project is as it was

    def test_card_menu_opens_the_labels_tab(self):
        shown: list[BoardSettings] = []
        with mock.patch.object(BoardSettings, "exec", lambda dialog: shown.append(dialog)):
            menu = self.window._task_menu(self._task("Доделать загрузку."))
            _item(menu, "Метки", "Настроить метки…").trigger()
        dialog = shown[0]
        self.assertIs(dialog.pages.currentWidget(), dialog.labels)

    def test_enter_in_a_name_only_finishes_it(self):
        """Enter in a field of a dialog would press its default button: a new column or Close."""
        for field in (self.page.names[tasks.TODO], self._labels_page_field()):
            field.setFocus()
            field.setText("Очередь")
            field.setModified(True)
            QTest.keyClick(field, Qt.Key.Key_Return)
            app.processEvents()
        self.assertEqual(len(self._keys()), 4)
        self.assertEqual([m.name for m in tasks.labels(self.recordings)], ["Очередь"])
        self.assertTrue(self.dialog.isVisible())

    def _labels_page_field(self):
        tasks.add_label(self.recordings, "Склад")
        self.dialog.show_tab(LABELS)
        self.dialog.labels.refresh()
        return self.dialog.labels.names["Склад"]


class TestColumnsTab(SettingsCase):
    def test_dragged_row_puts_the_column_there_and_the_board_follows(self):
        self.page.list_.moved.emit(self.review.key, 0)
        app.processEvents()
        self.assertEqual(self._keys()[0], self.review.key)
        self.assertEqual(self.page.list_.item(0).data(KEY), self.review.key)
        self.assertEqual(list(self.window.columns)[0], self.review.key)
        self.page.list_.moved.emit(tasks.TODO, 3)  # to the very end
        app.processEvents()
        self.assertEqual(self._keys()[-1], tasks.TODO)

    def test_name_typed_in_place_and_a_refused_one_goes_back(self):
        field = self.page.names[tasks.TODO]
        field.setText("Очередь")
        field.setModified(True)
        field.editingFinished.emit()
        app.processEvents()
        self.assertEqual(projects.column_name(projects.columns(self.recordings, "Склад")[0]), "Очередь")
        field.setText("готово")
        field.setModified(True)
        with mock.patch.object(style, "ask") as refusal:
            field.editingFinished.emit()
            field.editingFinished.emit()  # the message took the focus: finished again, asked once
        self.assertEqual(refusal.call_count, 1)
        self.assertIn("уже есть", refusal.call_args.args[2])
        self.assertEqual(field.text(), "Очередь")

    def test_category_from_the_row_moves_its_cards_state(self):
        finish = self._task("Доделать загрузку.")
        tasks.reorder(self.recordings, tasks.IN_PROGRESS, [finish.key], self.review.key)
        kind, _trash = self._row_buttons(self.review.key)
        _item(kind.menu(), "Готово").trigger()
        app.processEvents()
        self.assertEqual(projects.columns(self.recordings, "Склад")[2].base, tasks.DONE)
        self.assertEqual(self._task("Доделать загрузку.").status, tasks.DONE)

    def test_last_column_of_a_category_keeps_it_and_stays(self):
        for button in self._row_buttons(tasks.DONE):
            self.assertFalse(button.isEnabled())
            self.assertIn("Последняя колонка вида «Готово»", button.toolTip())
        self.assertTrue(all(b.isEnabled() for b in self._row_buttons(self.review.key)))

    def test_new_column_in_progress_with_a_free_name_to_type_over(self):
        self.page._add()
        self.page._add()
        added = projects.columns(self.recordings, "Склад")[3:5]  # after "На проверке", the last "In progress" one
        self.assertEqual([projects.column_name(c) for c in added], ["Новая колонка", "Новая колонка 2"])
        self.assertTrue(all(c.base == tasks.IN_PROGRESS for c in added))
        self.assertEqual(self.page.names[added[1].key].selectedText(), "Новая колонка 2")

    def test_delete_asks_where_the_cards_go_or_just_confirms(self):
        finish = self._task("Доделать загрузку.")
        tasks.reorder(self.recordings, tasks.IN_PROGRESS, [finish.key], self.review.key)
        done = projects.columns(self.recordings, "Склад")[-1]
        with mock.patch.object(board_columns_page, "ask_target", return_value=None):
            self._row_buttons(self.review.key)[1].click()
        self.assertIn(self.review.key, self._keys())  # cancelled
        with mock.patch.object(board_columns_page, "ask_target", return_value=done) as where:
            self._row_buttons(self.review.key)[1].click()
        app.processEvents()
        self.assertEqual(where.call_args.args[3], 1)  # one card
        self.assertNotIn(self.review.key, self._keys())
        self.assertEqual(self._task("Доделать загрузку.").column, done.key)
        waiting = projects.add_column(self.recordings, "Склад", "Ждет клиента", tasks.TODO)
        self.page.refresh()
        with mock.patch.object(style, "ask", return_value=True) as question:
            self._row_buttons(waiting.key)[1].click()
        app.processEvents()
        self.assertIn("Удалить колонку «Ждет клиента»?", question.call_args.args[2])
        self.assertNotIn(waiting.key, self._keys())

    def test_cards_go_by_default_to_the_first_other_column_of_the_category(self):
        list_ = projects.columns(self.recordings, "Склад")
        others = [c for c in list_ if c.key != self.review.key]
        with mock.patch.object(QDialog, "exec", return_value=QDialog.DialogCode.Accepted):
            target = board_columns_page.ask_target(self.dialog, list_[2], others, 2)
        self.assertEqual(target.key, tasks.IN_PROGRESS)
        with mock.patch.object(QDialog, "exec", return_value=QDialog.DialogCode.Rejected):
            self.assertIsNone(board_columns_page.ask_target(self.dialog, list_[2], others, 2))


class TestLabelsTab(SettingsCase):
    def setUp(self):
        super().setUp()
        tasks.add_label(self.recordings, "Склад", 0)
        tasks.add_label(self.recordings, "Срочно", 1)
        tasks.edit(self.recordings, self._task("Доделать загрузку.").key, tasks.Edit(labels=("Склад",)))
        self.dialog.show_tab(LABELS)
        self.labels = self.dialog.labels
        self.labels.refresh()

    def _row(self, name: str) -> QWidget:
        return self.labels.names[name].parentWidget()

    def test_rows_show_how_many_cards_have_the_label(self):
        self.assertEqual(list(self.labels.names), ["Склад", "Срочно"])
        for name, count in (("Склад", "1 карточка"), ("Срочно", "0 карточек")):
            self.assertIn(count, [m.text() for m in self._row(name).findChildren(QLabel)])

    def test_name_typed_in_place_goes_to_the_cards_and_the_board(self):
        field = self.labels.names["Склад"]
        field.setText("Остатки")
        field.setModified(True)
        field.editingFinished.emit()
        app.processEvents()
        self.assertEqual([m.name for m in tasks.labels(self.recordings)], ["Остатки", "Срочно"])
        self.assertEqual(self._task("Доделать загрузку.").labels, ("Остатки",))
        self.assertEqual([m.name for m in self.window._labels], ["Остатки", "Срочно"])  # the board follows
        field.setText("Срочно")
        field.setModified(True)
        with mock.patch.object(style, "ask") as refusal:
            field.editingFinished.emit()
        self.assertIn("уже есть", refusal.call_args.args[2])
        self.assertEqual(field.text(), "Остатки")
        field.setText("Склад")  # the row keeps the stored name: a second rename works
        field.setModified(True)
        field.editingFinished.emit()
        self.assertEqual([m.name for m in tasks.labels(self.recordings)], ["Склад", "Срочно"])

    def test_color_picked_from_the_circle(self):
        color = self._row("Склад").findChildren(QToolButton)[0]
        self.assertEqual(color.toolTip(), "Цвет метки")
        palette = color.menu().actions()[0].defaultWidget().findChildren(QToolButton)
        self.assertEqual(len(palette), tasks.LABEL_COLORS)
        palette[3].click()
        app.processEvents()
        self.assertEqual(tasks.labels(self.recordings)[0], tasks.Label("Склад", 3))

    def test_new_label_with_a_free_name_to_type_over(self):
        self.labels._add()
        self.labels._add()
        self.assertEqual(
            [m.name for m in tasks.labels(self.recordings)], ["Склад", "Срочно", "Новая метка", "Новая метка 2"]
        )
        self.assertEqual(self.labels.names["Новая метка 2"].selectedText(), "Новая метка 2")

    def test_delete_takes_the_label_off_the_cards(self):
        trash = self._row("Склад").findChildren(QToolButton)[-1]
        with mock.patch.object(style, "ask", return_value=False):
            trash.click()
        self.assertEqual(len(tasks.labels(self.recordings)), 2)
        with mock.patch.object(style, "ask", return_value=True) as question:
            trash.click()
        app.processEvents()
        self.assertIn("Метка «Склад» уйдет со всех карточек", question.call_args.args[2])
        self.assertEqual([m.name for m in tasks.labels(self.recordings)], ["Срочно"])
        self.assertEqual(self._task("Доделать загрузку.").labels, ())


if __name__ == "__main__":
    unittest.main()
