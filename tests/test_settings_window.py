# SPDX-License-Identifier: GPL-3.0-only
"""Settings window: a change applies at once, without a "Save" button (as Microsoft recommends for Windows
settings); a text field shows "Saved", leaving the settings does not lose an unfinished field, the calendar link is
checked by a button, changing the language offers a restart."""

import datetime
import pathlib
import types
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication, QPushButton

from knotes import ics_calendar, app, providers, language
from knotes import settings
from knotes.ui import settings_calendar, settings_hotkeys, settings_program, settings_recognition
from knotes.ui import settings_window
from tests.shown_windows import shown_windows

program = QApplication.instance() or QApplication([])


def _immediately(_pool: object, work, on_result) -> None:
    """Like run_in_background, but at once: the result or the exception goes to on_result."""
    try:
        result = work()
    except Exception as error:  # noqa: BLE001 - the real queue catches the same way
        result = error
    on_result(result)


class TestSettingsWindow(unittest.TestCase):
    def setUp(self):
        self.store: dict[str, str] = {}  # instead of the Windows credential store: tests do not touch the real one
        for patcher in (
            mock.patch.object(settings, "key", lambda name, _variable="": self.store.get(name, "")),
            mock.patch.object(settings, "set_key", self.store.__setitem__),
            mock.patch.object(settings, "address_key", return_value=""),
            mock.patch.object(settings, "set_address_key"),
            mock.patch.object(settings_calendar, "run_in_background", _immediately),
            mock.patch.object(providers, "models", return_value=[]),
        ):  # model list without network
            patcher.start()
            self.addCleanup(patcher.stop)
        language.set_("ru")
        self.window = settings_window.SettingsWindow(settings.Settings())
        self.changes: list[settings.Settings] = []
        self.window.changed.connect(self.changes.append)

    def tearDown(self):
        self.window.deleteLater()

    def test_name_saved_with_mark_and_no_extra_writes(self):
        self.window.minutes.name.setText(" Дмитрий ")
        self.window.minutes.name.editingFinished.emit()
        self.assertEqual(self.changes[-1].my_name, "Дмитрий")
        self.assertFalse(self.window.minutes.name_saved.isHidden())
        self.window.minutes.name.editingFinished.emit()  # left the field without changes: nothing to save
        self.assertEqual(len(self.changes), 1)

    def test_leaving_settings_saves_unfinished_field(self):
        """Alt+← leaves the settings without taking focus off the field: the name and the link must be saved anyway."""
        self.window.minutes.name.setText("Дмитрий")
        self.window.meetings.calendar.secret.field.setText(" https://example.com/c.ics ")
        self.window.close()
        self.assertEqual(self.changes[-1].my_name, "Дмитрий")
        self.assertEqual(self.store[ics_calendar.KEY], "https://example.com/c.ics")

    def test_calendar_checked_by_button(self):
        start = datetime.datetime(2026, 10, 26, 10, 0)
        self.window.meetings.calendar.secret.field.setText("https://example.com/c.ics")
        event = ics_calendar.Event("Планерка", start, start + datetime.timedelta(minutes=30))
        with mock.patch.object(ics_calendar, "upcoming", return_value=[event]) as upcoming:
            self.window.meetings.calendar.secret.check.click()
        self.assertEqual(upcoming.call_args.args[0], "https://example.com/c.ics")
        self.assertEqual(self.store[ics_calendar.KEY], "https://example.com/c.ics")  # checking also saves
        self.assertIn("встреч на неделю: 1", self.window.meetings.calendar.secret.result.text())
        self.assertIn("Планерка, 26.10 10:00", self.window.meetings.calendar.secret.result.text())
        with mock.patch.object(ics_calendar, "upcoming", side_effect=providers.ProviderError("Сервер не ответил")):
            self.window.meetings.calendar.secret.check.click()
        self.assertEqual(self.window.meetings.calendar.secret.result.text(), "Сервер не ответил")
        self.window.meetings.calendar.secret.field.clear()
        self.window.meetings.calendar.secret.check.click()
        self.assertIn("Ссылка не задана", self.window.meetings.calendar.secret.result.text())

    def test_language_change_offers_restart(self):
        choice, restarts = self.window.program.ui_language, []
        self.window.restart.connect(lambda: restarts.append(1))
        self.assertTrue(self.window.program.restart_row.isHidden())
        choice.setCurrentIndex(choice.findData("en"))
        self.assertEqual(self.changes[-1].ui_language, "en")  # the choice is saved at once
        self.assertFalse(self.window.program.restart_row.isHidden())
        self.window.program.restart_now.click()
        self.assertEqual(restarts, [1])
        choice.setCurrentIndex(choice.findData("ru"))
        self.assertTrue(self.window.program.restart_row.isHidden())  # same language as now: no need to restart

    def test_section_by_code(self):
        """The help leads to a section by its code; the help itself is a section of the main window, not here."""
        self.assertEqual(self.window.sections.count(), len(settings_window.SECTIONS))
        self.window.show_section("протоколы")
        self.assertEqual(self.window.sections.currentItem().text(), "Протоколы")
        self.assertEqual(self.window.pages.currentIndex(), 1)

    def test_recognition_device_choice_is_saved_for_supported_models(self):
        device = self.window.recognition.local_model.device
        cpu, gpu = device.findChildren(QPushButton)
        self.assertTrue(gpu.isChecked())
        cpu.click()
        self.assertFalse(self.changes[-1].local_gpu)
        gpu.click()
        self.assertTrue(self.changes[-1].local_gpu)

        speech = self.window.connections["распознавание"]
        original_model = speech.chosen_model
        speech.set_model("whisper-large-v3-turbo-int8")
        self.assertTrue(cpu.isChecked())
        self.assertFalse(device.isEnabled())
        self.assertTrue(self.changes[-1].local_gpu)
        speech.set_model(original_model)
        self.assertTrue(device.isEnabled())
        self.assertTrue(gpu.isChecked())

    def test_opening_flashes_no_window(self):
        """Every field is placed in the window before it is shown, with any preset: the refresh button, the shared key
        note and the plain http warning."""
        own_server = settings.Settings(
            provider="свой",
            address="http://example.com/v1",
            minutes_service="свой",
            minutes_address="http://example.com/v1",
        )
        for config in (settings.Settings(), own_server):
            with shown_windows() as windows:
                settings_window.SettingsWindow(config).deleteLater()
            self.assertEqual(windows, [], config.provider)


class TestSettingsFailures(TestSettingsWindow):
    """A refused write says so at its setting and keeps what was typed; shortcuts do not clash; icons follow the
    theme; the vocabulary keeps its words however its window closes."""

    def test_shortcut_used_by_another_action_is_refused(self):
        editor = next(
            e for e in self.window.findChildren(settings_hotkeys.QKeySequenceEdit) if e.keySequence().toString()
        )  # the recording shortcut
        mark = [e for e in self.window.findChildren(settings_hotkeys.QKeySequenceEdit)][1]
        mark.setKeySequence(editor.keySequence())
        self.window.program.hotkeys.set_hotkey("mark_hotkey", mark)
        self.assertEqual(self.changes, [])
        self.assertIn("уже занято", self.window.program.hotkeys.result.text())
        self.assertEqual(self.window.program.hotkeys.result.objectName(), "ошибка_текст")
        shown = mark.keySequence().toString(settings_hotkeys.QKeySequence.SequenceFormat.PortableText)
        self.assertEqual(shown, settings.Settings().mark_hotkey)  # the field shows the shortcut that works

    def test_calendar_link_not_saved_says_why(self):
        self.window.meetings.calendar.secret.field.setText("https://example.com/c.ics")
        with mock.patch.object(settings, "set_key", side_effect=OSError(5, "Access is denied")):
            self.window.meetings.calendar.save()
        self.assertIn("не сохранил ссылку", self.window.meetings.calendar.secret.result.text())
        self.assertTrue(self.window.meetings.calendar.secret.saved.isHidden())
        self.window.meetings.calendar.save()  # written on the next try
        self.assertEqual(self.store[ics_calendar.KEY], "https://example.com/c.ics")

    def test_calendar_not_saved_is_not_checked(self):
        """A link the store refused is not checked: the line keeps saying why, the stored link is another one."""
        self.window.meetings.calendar.secret.field.setText("https://example.com/c.ics")
        with (
            mock.patch.object(settings, "set_key", side_effect=OSError(5, "Access is denied")),
            mock.patch.object(ics_calendar, "upcoming") as upcoming,
        ):
            self.window.meetings.calendar.secret.check.click()
        upcoming.assert_not_called()
        self.assertIn("не сохранил ссылку", self.window.meetings.calendar.secret.result.text())

    def test_calendar_check_of_sign_in_page(self):
        self.window.meetings.calendar.secret.field.setText("https://example.com/c.ics")
        with mock.patch.object(providers, "fetch_secret", return_value="<html><body>Войдите</body></html>"):
            self.window.meetings.calendar.secret.check.click()
        self.assertIn("не календарь ICS", self.window.meetings.calendar.secret.result.text())
        self.assertEqual(self.window.meetings.calendar.secret.result.objectName(), "ошибка_текст")

    def test_autostart_refused_by_windows_goes_back(self):
        switch = self.window.program.autostart_result.parent().findChild(settings_window.style.Switch)
        before = switch.isChecked()
        with mock.patch.object(settings_program.system, "set_autostart", side_effect=PermissionError(5, "denied")):
            switch.click()
        self.assertEqual(switch.isChecked(), before)
        self.assertFalse(self.window.program.autostart_result.isHidden())
        self.assertIn("не дала", self.window.program.autostart_result.text())

    def test_vocabulary_kept_when_closed_by_cross(self):
        def typed_and_closed(window):
            window._add("Claude", "клод, cloud")
            window.reject()  # the cross or Esc
            return 0

        with mock.patch.object(settings_recognition.VocabularyDialog, "exec", typed_and_closed):
            self.window.recognition.open_vocabulary()
        self.assertEqual(self.changes[-1].vocabulary, [{"правильно": "Claude", "варианты": ["клод", "cloud"]}])

    def test_icons_follow_theme(self):
        red = settings_window.QColor("#FF0000")
        with (
            mock.patch.object(settings_window, "_ink", return_value=red),
            mock.patch.object(settings_window.style, "icon", wraps=settings_window.style.icon) as drawn,
        ):
            self.window.repaint_theme()  # the theme changed
        signs = {c.args[0] for c in drawn.call_args_list if c.args[1] == red}
        style = settings_window.style
        for sign in (style.MICROPHONE, style.SETTINGS, style.BOOK, style.DOCUMENT, style.PLUS):
            self.assertIn(sign, signs)


class TestOpenMeetings(unittest.TestCase):
    def test_closed_meeting_is_forgotten_not_a_newer_one(self):
        """A closed meeting leaves the open ones; a meeting closed after the same one was opened again does not take
        the new one with it."""
        folder, old, new = pathlib.Path("встреча"), object(), object()
        program_ = types.SimpleNamespace(_transcript_windows={folder: new})
        app.App._forget_meeting(program_, folder, old)
        self.assertIs(program_._transcript_windows[folder], new)
        app.App._forget_meeting(program_, folder, new)
        self.assertEqual(program_._transcript_windows, {})


class TestRestart(unittest.TestCase):
    def _program(self, **taken: object) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            **({"recording": None, "busy_items": {}, "minutes_list": set()} | taken), quit_app=mock.Mock()
        )

    def test_new_copy_and_exit(self):
        program_ = self._program()
        with mock.patch.object(app.system, "launch_copy") as startup:
            app.App.restart(program_)
        startup.assert_called_once_with("--restart", "--settings")
        program_.quit_app.assert_called_once()

    def test_recording_and_recognition_not_interrupted(self):
        for taken in ({"recording": object()}, {"busy_items": {"п": "Распознается"}}, {"minutes_list": {"п"}}):
            program_ = self._program(**taken)
            with (
                self.subTest(taken=taken),
                mock.patch.object(app.system, "launch_copy") as startup,
                mock.patch.object(app.QMessageBox, "information") as message,
            ):
                app.App.restart(program_)
            startup.assert_not_called()
            message.assert_called_once()
            program_.quit_app.assert_not_called()


if __name__ == "__main__":
    unittest.main()
