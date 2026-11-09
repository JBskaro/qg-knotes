# SPDX-License-Identifier: GPL-3.0-only
"""Meeting view: phrase font size and search arrows."""

import contextlib
import errno
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QMessageBox, QPushButton, QScrollBar

from knotes import catalog, minutes, notes, transcript, kinds
from knotes.ui import export_dialog, style
from knotes.ui import meeting_view
from knotes.ui import minutes_view as minutes_window
from knotes.ui.meeting_view import MINUTES, NOTES, TRANSCRIPT, MeetingView

app = QApplication.instance() or QApplication([])


class TestMeetingView(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        folder = pathlib.Path(self._temporary.name) / "2026-10-14_15-00"
        folder.mkdir()
        transcript.save(
            folder,
            [
                {"начало": 2.0, "конец": 4.0, "кто": "Я", "текст": "Смотрим загрузку."},
                {"начало": 5.0, "конец": 7.0, "кто": "Собеседник 1", "текст": "Нарядили ёлку."},
            ],
            {"длительность": 60, "провайдер": "OpenRouter", "стоимость": 0.1},
            [],
        )
        self.window = MeetingView(folder)
        self.window.show()
        app.processEvents()

    def tearDown(self):
        self.window.close()
        self._temporary.cleanup()

    def _phrase_size(self) -> int:
        caption = self.window._rows[0].caption
        caption.ensurePolished()
        return caption.fontInfo().pixelSize()

    def test_closing_right_after_a_place_from_the_minutes(self):
        """The highlight of a phrase is taken off later; a meeting closed before that raises no error."""
        errors: list[BaseException] = []
        with (
            mock.patch.object(meeting_view, "HIGHLIGHT_MS", 50),
            mock.patch.object(sys, "excepthook", lambda _kind, error, _trace: errors.append(error)),
        ):
            view = MeetingView(self.window.folder)
            view.show()
            app.processEvents()
            view.show_place(2.0)
            view.close()
            view.deleteLater()
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
            end = time.monotonic() + 0.3
            while time.monotonic() < end:
                app.processEvents()
        self.assertEqual(errors, [])

    def test_closing_says_so(self):
        """The program forgets a closed meeting at once, so it can be opened again right away."""
        closed: list[bool] = []
        self.window.closed.connect(lambda: closed.append(True))
        self.window.close()
        self.assertEqual(closed, [True])

    def test_header_fits_a_narrow_column(self):
        """The project chooser is on the line with the date, so the meeting fits a narrow column; a long title shows
        its beginning."""
        self.assertLess(self.window.minimumSizeHint().width(), 480)
        catalog.set_(self.window.folder, title="Согласование загрузки остатков по складам и магазинам")
        self.window.resize(420, 600)
        self.window.refresh()
        app.processEvents()
        self.assertEqual(self.window.title.cursorPosition(), 0)

    def test_size_buttons_change_phrase_font(self):
        self.window.show_tab(TRANSCRIPT)
        before = self._phrase_size()
        self.window._font_size(1)
        self.window._font_size(1)
        app.processEvents()
        self.assertGreater(self._phrase_size(), before)
        for _ in range(4):
            self.window._font_size(-1)
        app.processEvents()
        self.assertLess(self._phrase_size(), before)

    def _visible_items(self) -> tuple[bool, bool]:
        app.processEvents()
        return self.window.minutes.isVisible(), self.window.transcript_panel.isVisible()

    def test_opens_on_the_tab_asked_for_or_the_closest_one(self):
        """The tab of the meeting before; a transcript that can't be built (the folder is held, the notes file is
        not in UTF-8) does not keep the meeting from opening: it opens on the minutes."""
        view = MeetingView(self.window.folder, tab=NOTES)
        self.addCleanup(view.close)
        self.assertEqual(view.tab(), NOTES)
        held = PermissionError(errno.EACCES, "Access is denied", "заметки.md")
        with (
            mock.patch.object(meeting_view.notes, "with_minutes", side_effect=held),
            self.assertLogs(meeting_view.log, "WARNING"),
        ):
            view = MeetingView(self.window.folder, tab=TRANSCRIPT)
        self.addCleanup(view.close)
        self.assertEqual(view.tab(), MINUTES)
        view.show_tab(TRANSCRIPT)  # the tab itself is built again when chosen
        self.assertEqual(view.tab(), TRANSCRIPT)
        self.assertTrue(view._rows)

    def test_minutes_and_transcript_tabs(self):
        """Minutes by default, the transcript on the next tab; a click on a time in the minutes and the search
        switch to the transcript."""
        self.assertEqual((self.window.tab(), self._visible_items()), (MINUTES, (True, False)))
        self.assertEqual(self.window.title.focusPolicy(), Qt.FocusPolicy.ClickFocus)  # typing won't rename the meeting
        self.window.switch_tab()  # Ctrl+3
        self.assertEqual((self.window.tab(), self._visible_items()), (TRANSCRIPT, (False, True)))
        self.window.show_tab(MINUTES)
        self.window.show_place(2)
        self.assertEqual(self._visible_items(), (False, True))
        self.window.show_tab(MINUTES)
        self.window._to_search()  # Ctrl+F from the minutes
        self.assertEqual(self._visible_items(), (False, True))
        self.assertTrue(self.window.search.hasFocus())

    def test_minutes_sections_as_blocks_and_time_as_link(self):
        (self.window.folder / minutes.FILE).write_text(
            "# Итоги\n\nУчастники - Дмитрий\n\n## Решения\n\n1. Цена из прайса. [05:40]\n\n## Задачи\n\n"
            "1. Доделать загрузку. Игорь. [09:10]\n2. Проверить коэффициент. Дмитрий.\n\n"
            "## Для аналитика\n\n1. К задаче 1. Задвоение. [11:10]\n",
            encoding="utf-8",
        )
        kind = self.window.minutes
        kind.display()
        blocks = kind.sheet.findChildren(QFrame, "раздел")
        self.assertEqual(
            [b.findChild(QLabel, "заголовок_раздела").text() for b in blocks], ["Решения", "Задачи", "Для аналитика"]
        )
        self.assertEqual(blocks[1].findChild(QLabel, "пунктов").text(), "2")
        self.assertIn("Игорь.", blocks[1].findChild(QLabel, "текст_раздела").text())  # who is separate, in bold
        self.assertEqual(
            [d.text() for d in kind._copy_button.menu().actions()],
            ["Письмо", "Для базы знаний", "", "Сохранить файлом (PDF, Word)…"],
        )
        places: list[float] = []
        kind.to_place.connect(places.append)
        blocks[0].findChild(QLabel, "текст_раздела").linkActivated.emit("#340")
        self.assertEqual(places, [340.0])

    def test_english_minutes_shown_as_russian(self):
        """Participants label in the minutes' own language, who and when apart, the analyst section dashed."""
        (self.window.folder / minutes.FILE).write_text(
            "# Minutes\n\nParticipants - Dmitry\n\n## Tasks\n\n1. Finish the import. Igor. [09:10]\n\n"
            "Transcript - 2026-10-14_15-00, расшифровка.md\n\n## For the analyst\n\n1. On task 1. Twice. [11:10]\n",
            encoding="utf-8",
        )
        view = self.window.minutes
        view.display()
        start = view.sheet.findChildren(QLabel)[0].text()
        self.assertIn("Participants</span>", start)
        self.assertNotIn("Участники", start)
        blocks = view.sheet.findChildren(QFrame, "раздел")
        self.assertEqual(["dashed" in b.styleSheet() for b in blocks], [False, True])
        tasks_text = blocks[0].findChild(QLabel, "текст_раздела").text()
        self.assertIn("font-weight:600'>Igor.</span>", tasks_text)
        self.assertIn("font-size:12px; margin:4px 0 0 0'>Transcript - ", tasks_text)

    def test_kind_chosen_before_composing_and_again(self):
        """The meeting kind is by the "Write minutes" button; the chosen one is remembered in the meeting and used
        to write again."""
        kind = self.window.minutes
        self.assertEqual(kind.meeting_kind.currentText(), kinds.WORKING)
        self.assertEqual(kind.meeting_kind.count(), len(kinds.kinds()))
        kind.meeting_kind.setCurrentText("Показ доработки")
        fed: list[str] = []
        kind.compose.connect(lambda: fed.append(catalog.properties(kind.folder).get("вид")))
        kind._button.click()
        self.assertEqual(fed, ["Показ доработки"])
        (kind.folder / minutes.FILE).write_text("# Итоги\n\n## Замечания\n\n1. Нет итога. [01:00]\n", encoding="utf-8")
        kind.display()
        with mock.patch.object(minutes_window.QInputDialog, "getItem", return_value=("Обследование", True)) as choice:
            kind._compose_again()
        self.assertEqual(choice.call_args.args[4], 2)  # the kind used before is offered: "Показ доработки"
        self.assertEqual(fed, ["Показ доработки", "Обследование"])
        with mock.patch.object(minutes_window.QInputDialog, "getItem", return_value=("", False)):
            kind._compose_again()  # changed their mind: nothing is written
        self.assertEqual(len(fed), 2)

    def test_card_scrollbars_are_native(self):
        """The card style sheet makes the scroll area transparent but leaves the scroll bars alone: otherwise Qt
        draws them classic, with a grid and arrows."""
        (self.window.folder / minutes.FILE).write_text(
            "# Итоги\n\n## Решения\n\n" + "".join(f"{n}. Решение номер {n}. [00:0{n % 10}]\n" for n in range(1, 60)),
            encoding="utf-8",
        )
        self.window.minutes.display()
        card = style.layer(self.window)
        card.setStyleSheet(style.window_style())
        card.show()
        app.processEvents()
        scrollbars = card.findChildren(QScrollBar)
        self.assertTrue(scrollbars)
        for scrollbar in scrollbars:
            with self.subTest(scrollbar=scrollbar.parent().objectName()):
                self.assertFalse(scrollbar.testAttribute(Qt.WidgetAttribute.WA_StyleSheetTarget))
        self.window.setParent(None)  # tearDown closes the meeting view: the card must not delete it along with itself
        card.deleteLater()

    def test_search_arrows_only_with_matches(self):
        self.window.show_tab(TRANSCRIPT)
        app.processEvents()
        self.assertTrue(all(not s.isVisible() for s in self.window._arrows))
        self.window.search.setText("загрузку")
        self.window._search_delay.timeout.emit()
        app.processEvents()
        self.assertTrue(all(s.isVisible() for s in self.window._arrows))
        self.assertEqual(self.window.found.text(), "1 из 1")

    def test_transcript_built_when_its_tab_is_first_shown(self):
        """A meeting opens on the minutes: the transcript of thousands of phrases is not built until it is looked
        at, nor again without a change."""
        self.assertEqual(self.window._rows, [])
        with mock.patch.object(meeting_view.transcript, "names", wraps=transcript.names) as names:
            self.window.show_tab(TRANSCRIPT)
        self.assertEqual(len(self.window._rows), 2)
        self.assertEqual(names.call_count, 1)  # once for the whole transcript, not per block
        rows = list(self.window._rows)
        self.window.show_tab(NOTES)
        self.window.show_tab(TRANSCRIPT)
        self.assertEqual(self.window._rows, rows)

    def test_name_typed_shows_without_rebuilding(self):
        self.window.show_tab(TRANSCRIPT)
        rows = list(self.window._rows)
        self.window._rename("Собеседник 1", "Анна")
        self.assertEqual(self.window._rows, rows)
        chips = [label.text() for label in self.window.text.findChildren(QLabel)]
        self.assertIn("Анна", chips)
        self.assertEqual(transcript.names(self.window.folder), {"Собеседник 1": "Анна"})
        self.window.refresh()
        self.assertEqual(self.window._rows, rows)

    def test_name_typed_keeps_a_name_the_assistant_gave_meanwhile(self):
        """Only the typed name is written: a name the AI assistant set a moment ago for another participant stays."""
        self.window.show_tab(TRANSCRIPT)
        transcript.set_names(self.window.folder, {"Собеседник 2": "Игорек"})
        before = transcript.names(self.window.folder)  # what the window read before the assistant
        transcript.set_names(self.window.folder, {"Собеседник 2": "Игорь"})  # the assistant, over MCP
        with mock.patch.object(transcript, "names", return_value=before):
            self.window._rename("Собеседник 1", "Анна")
        self.assertEqual(transcript.names(self.window.folder), {"Собеседник 1": "Анна", "Собеседник 2": "Игорь"})

    def test_minutes_edit_written_whole_under_the_folder_lock(self):
        """The edit of the minutes replaces the file whole, while no other edit of the meeting goes on."""
        (self.window.folder / minutes.FILE).write_text("# Итоги\n", encoding="utf-8")
        view = self.window.minutes
        view.display()
        view._toggle_edit()
        view.edit_item.setPlainText("# Итоги\n\n1. Правка.\n")
        events: list[str] = []
        real_locked = transcript.locked

        @contextlib.contextmanager
        def locked(path):
            with real_locked(path):
                events.append("lock")
                yield
                events.append("unlock")

        with (
            mock.patch.object(transcript, "locked", locked),
            mock.patch.object(transcript, "write_text", side_effect=lambda *a: events.append("write")),
        ):
            self.assertTrue(view.save())
        self.assertEqual(events, ["lock", "write", "unlock"])

    def test_search_waits_for_typing_and_treats_e_and_yo_alike(self):
        self.window.show_tab(TRANSCRIPT)
        self.window.search.setText("елку")
        self.assertEqual(self.window.found.text(), "")  # not on every letter
        self.assertTrue(self.window._search_delay.isActive())
        self.window._search_delay.timeout.emit()
        self.assertEqual(self.window.found.text(), "1 из 1")
        self.assertIn("background", self.window._rows[1].caption.text())

    def test_note_text_shown_as_typed(self):
        """A note "P&L <b>план</b>" is text: not markup, and the ampersand is not swallowed by the button."""
        notes.add(self.window.folder, 3.0, "P&L <b>план</b>")
        self.window.show_tab(TRANSCRIPT)
        label = next(m for m in self.window.text.findChildren(QLabel) if "P&L" in m.text())
        self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn("P&L <b>план</b>", label.text())
        link = next(b for b in self.window.panel.findChildren(QPushButton) if "P&&L" in b.text())
        self.assertIn("&lt;b&gt;", link.toolTip())

    def test_minutes_edit_saved_by_itself(self):
        """An edit of the minutes is saved while typing, as in the notepad, not only when the meeting closes."""
        (self.window.folder / minutes.FILE).write_text("# Итоги\n", encoding="utf-8")
        view = self.window.minutes
        view.display()
        view._toggle_edit()
        view.edit_item.setPlainText("# Итоги\n\n## Решения\n\n1. Правка.\n")
        self.assertTrue(view._save_later.isActive())
        view._save_later.timeout.emit()
        self.assertIn("Правка", (self.window.folder / minutes.FILE).read_text(encoding="utf-8"))

    def test_closing_when_the_edit_cannot_be_saved_asks(self):
        """A full disk: closing asks whether to close without saving; "No" keeps the meeting and the edit, "Yes"
        closes it and the program is told, so the meeting opens again with a click."""
        (self.window.folder / minutes.FILE).write_text("# Итоги\n", encoding="utf-8")
        view = self.window.minutes
        view.display()
        view._toggle_edit()
        view.edit_item.setPlainText("# Итоги\n\n1. Не потерять.\n")
        closed: list[bool] = []
        self.window.closed.connect(lambda: closed.append(True))
        full = OSError(errno.ENOSPC, "No space left on device", str(view.file))
        no, yes = QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes
        with mock.patch.object(transcript, "write_text", side_effect=full):
            with mock.patch.object(meeting_view.QMessageBox, "question", return_value=no) as asked:
                self.assertFalse(self.window.close())
            self.assertIn("закончилось место", asked.call_args.args[2])
            self.assertEqual(closed, [])
            self.assertEqual(view.edit_item.toPlainText(), "# Итоги\n\n1. Не потерять.\n")
            with mock.patch.object(meeting_view.QMessageBox, "question", return_value=yes):
                self.assertTrue(self.window.close())
        self.assertEqual(closed, [True])

    def test_notes_that_cannot_be_saved_are_asked_about(self):
        closed: list[bool] = []
        self.window.closed.connect(lambda: closed.append(True))
        with (
            mock.patch.object(self.window.notepad, "save", side_effect=PermissionError(13, "Access is denied")),
            mock.patch.object(meeting_view.QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes),
        ):
            self.assertTrue(self.window.close())
        self.assertEqual(closed, [True])

    def test_email_keeps_angle_brackets_as_text(self):
        """The text "Change type <String> to <Number>" stays in the email; HTML and pictures in the minutes do not
        come alive in the mail."""
        data = minutes_window.email_data(
            "# Итоги\n\n## Решения\n\n1. Change type <String> to <Number>. <img src='file:///C:/x.png'> "
            "![счетчик](http://example.com/p.png)\n"
        )
        self.assertIn("<String>", data.text())
        self.assertIn("&lt;String&gt;", data.html())
        self.assertNotIn("<img", data.html())

    def _dialog(self, name: str, kind: str) -> mock._patch:
        path = str(pathlib.Path(self._temporary.name) / name)
        return mock.patch.object(export_dialog.QFileDialog, "getSaveFileName", return_value=(path, kind))

    def test_minutes_saved_as_word_or_pdf(self):
        """Saving as a file is in the copy menu: the name comes from the meeting, the type chosen in the dialog
        decides the format of a name typed without an extension."""
        (self.window.folder / minutes.FILE).write_text(
            "# Итоги\n\n## Задачи\n\n1. Доделать загрузку. Игорь. [09:10]\n", encoding="utf-8"
        )
        view = self.window.minutes
        view.display()
        item = next(a for a in view._copy_button.menu().actions() if a.text().startswith("Сохранить файлом"))
        with self._dialog("итоги", "Документ Word (*.docx)") as asked:
            item.trigger()
        self.assertTrue(asked.call_args.args[2].endswith("Протокол - Встреча 14.10 15-00 - 14.10.2026.pdf"))
        saved = pathlib.Path(self._temporary.name) / "итоги.docx"
        self.assertTrue(saved.read_bytes().startswith(b"PK"))
        self.assertEqual(view.result.text(), "Сохранено: итоги.docx")
        with self._dialog("итоги", "Документ PDF (*.pdf)"):
            view._save_file()
        self.assertTrue((pathlib.Path(self._temporary.name) / "итоги.pdf").read_bytes().startswith(b"%PDF"))

    def test_transcript_saved_as_a_file(self):
        with self._dialog("расшифровка.pdf", "Документ PDF (*.pdf)") as asked:
            self.window._save_file()
        self.assertIn("Расшифровка - Встреча 14.10", asked.call_args.args[2])
        self.assertTrue((pathlib.Path(self._temporary.name) / "расшифровка.pdf").exists())
        self.assertEqual(self.window.found.text(), "Сохранено: расшифровка.pdf")

    def test_file_in_use_is_reported(self):
        held = PermissionError(13, "Access is denied")
        with (
            self._dialog("расшифровка.docx", "Документ Word (*.docx)"),
            mock.patch.object(export_dialog.export, "save", side_effect=held),
            mock.patch.object(export_dialog.QMessageBox, "warning") as told,
            self.assertLogs(export_dialog.log, "WARNING"),
        ):
            self.window._save_file()
        self.assertIn("открыт в другой программе", told.call_args.args[2])
        with mock.patch.object(export_dialog.QFileDialog, "getSaveFileName", return_value=("", "")):
            self.window._save_file()  # changed their mind

    def test_meeting_not_transcribed_has_nothing_to_save(self):
        (self.window.folder / transcript.PHRASES).unlink()
        with mock.patch.object(export_dialog.QFileDialog, "getSaveFileName") as asked:
            self.window._save_file()
        asked.assert_not_called()
        self.assertEqual(self.window.problem.text(), "Встреча еще не распознана")


if __name__ == "__main__":
    unittest.main()
