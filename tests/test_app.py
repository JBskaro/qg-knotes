# SPDX-License-Identifier: GPL-3.0-only
"""The program shell: stopping a recording on a full disk, the list refresh, the meeting tab, the quit order."""

import dataclasses
import errno
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication, QMessageBox

from knotes import app, catalog, failures, meeting_list, minutes, settings, transcript
from knotes.meeting_list import Busy
from knotes.ui.meeting_view import MINUTES, NOTES, TRANSCRIPT

qt = QApplication.instance() or QApplication([])
FULL = OSError(errno.ENOSPC, "No space left on device", "заметки.md")


def program_for(test: unittest.TestCase) -> app.App:
    """The program with its settings in a temporary folder; toasts are collected instead of shown."""
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    root = pathlib.Path(temporary.name)
    config = settings.Settings(recordings_folder=str(root / "записи"))
    config.recordings.mkdir()
    for patch in (
        mock.patch.object(app.settings, "read", return_value=(config, "")),
        mock.patch.object(app.settings, "settings_file", return_value=root / "настройки.json"),
        mock.patch.object(app.App, "notify"),  # before the start: the session and the updates get it too
    ):
        patch.start()
        test.addCleanup(patch.stop)
    program = app.App(on_autostart=True)
    program._notify_with_buttons = mock.Mock()
    for timer in program._timers:
        timer.stop()
    test.addCleanup(program.window.deleteLater)
    return program


class TestStopOnFullDisk(unittest.TestCase):
    def setUp(self):
        self.program = program_for(self)
        self.folder = self.program.settings.recordings / "2026-10-27_10-00"
        self.folder.mkdir()
        self.session = self.program.session
        self.session.recording = mock.Mock(folder=self.folder, duration=mock.Mock(return_value=12.0))
        self.session.line = mock.Mock()
        self.notepad = mock.Mock(folder=self.folder)
        self.notepad.notepad.save.return_value = False  # the notepad keeps the text and shows the reason itself
        self.notepad.notepad.failure.text.return_value = failures.text(FULL)
        self.session._notepad = self.notepad

    def test_stop_stops_even_if_notes_are_not_saved(self):
        """A full disk does not keep the recording going: it stops, the bar closes, the notepad stays open with its
        text and the user is told why."""
        recording_ = self.session.recording
        self.session.stop()
        recording_.stop.assert_called_once()
        self.assertIsNone(self.session.line)
        self.assertIsNone(self.session._notepad)  # the next recording gets a notepad of its own
        self.assertEqual(self.session._kept_notepads, [self.notepad])
        self.notepad.close.assert_not_called()
        self.notepad.display.assert_called_once()

    def test_end_resets_the_state_whatever_fails(self):
        """The end of a recording resets the state first: the icon is not left red, the next recording can start."""
        with (
            mock.patch.object(app.catalog, "set_", side_effect=FULL),
            mock.patch.object(self.program, "_show_recording") as shown,
        ):
            self.session._on_end(self.folder, "")
        self.assertIsNone(self.program.recording)
        shown.assert_called_with(False)
        self.assertNotIn(self.folder, self.program.busy_items)
        texts = " ".join(c.args[1] for c in self.program.notify.call_args_list)
        self.assertIn("закончилось место", texts)

    def test_end_saves_notes_and_duration(self):
        self.notepad.notepad.save.return_value = True
        self.session._recorded_seconds = 12.0
        self.session._on_end(self.folder, "")
        self.notepad.close.assert_called_once()
        self.assertIsNone(self.session._notepad)
        self.assertEqual(catalog.properties(self.folder)["секунд"], 12.0)


def wait_for(condition: object, seconds: float = 5.0) -> None:
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        qt.processEvents()
        time.sleep(0.01)


class TestListRefresh(unittest.TestCase):
    def setUp(self):
        self.program = program_for(self)

    def test_list_out_of_sight_is_not_read(self):
        """Every 5 seconds the list is reread only while it is in sight: not in a hidden or minimized window, not
        under a section page."""
        with mock.patch.object(app.meeting_list, "read") as read:
            self.program._refresh_window()
        read.assert_not_called()

    def test_list_read_in_background_one_reading_at_a_time(self):
        """The window thread does not wait for the meeting folders; a refresh asked for during a reading comes after
        it."""
        gate, threads = threading.Event(), []

        def slow(recordings: pathlib.Path, priced: bool) -> meeting_list.Snapshot:
            threads.append(threading.current_thread())
            gate.wait(5)
            return meeting_list.Snapshot([], None, 3)

        with (
            mock.patch.object(app.meeting_list, "read", side_effect=slow) as read,
            mock.patch.object(self.program.window, "show_meetings") as shown,
            mock.patch.object(self.program.window, "set_tasks") as counted,
        ):
            self.program._refresh_window(always=True)
            self.program._refresh_window(always=True)
            gate.set()
            wait_for(lambda: shown.call_count == 2)
        self.assertEqual(read.call_count, 2)
        self.assertNotIn(threading.main_thread(), threads)
        counted.assert_called_with(3)


class TestMeetingTab(unittest.TestCase):
    def setUp(self):
        self.program = program_for(self)
        patch = mock.patch.object(self.program, "show_window")  # the window stays off the screen
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.program.window.close_meeting)

    def _meeting(self, name: str, transcribed: bool = True) -> pathlib.Path:
        folder = self.program.settings.recordings / name
        folder.mkdir()
        (folder / "микрофон.flac").write_bytes(b"x")
        if transcribed:
            transcript.save(
                folder,
                [{"начало": 1.0, "конец": 2.0, "кто": "Я", "текст": "Начнем."}],
                {"длительность": 60, "провайдер": "OpenRouter", "стоимость": 0.1},
                [],
            )
        return folder

    def test_next_meeting_opens_on_the_tab_of_the_previous_one(self):
        """The notes stay the notes when another meeting is chosen, the transcript stays the transcript; a meeting
        not transcribed yet has no transcript and opens on the minutes, where it says so; the next transcribed one
        is on the transcript again."""
        first, second, third = (self._meeting(f"2026-10-27_1{n}-00") for n in range(3))
        recorded = self._meeting("2026-10-27_14-00", transcribed=False)
        view = self.program.open_meeting(first)
        self.assertEqual(view.tab(), MINUTES)
        view.show_tab(NOTES)
        self.assertEqual(self.program.open_meeting(second).tab(), NOTES)
        self.program.window.meeting.switch_tab()  # Ctrl+3 from the notes, as before: the transcript
        self.assertEqual(self.program.window.meeting.tab(), TRANSCRIPT)
        view = self.program.open_meeting(recorded)
        self.assertEqual(view.tab(), MINUTES)
        self.assertFalse(view.minutes._button.isEnabled())  # "Transcribe the meeting first"
        self.assertEqual(self.program.open_meeting(third).tab(), TRANSCRIPT)

    def test_minutes_button_and_place_from_the_minutes_as_before(self):
        """No minutes yet: the minutes tab with its "Write minutes" button; a click on a time in the minutes opens
        the transcript, and the next meeting opens there too."""
        first, second = self._meeting("2026-10-27_10-00"), self._meeting("2026-10-27_11-00")
        view = self.program.open_meeting(first)
        self.assertTrue(view.minutes._button.isEnabled())
        view.minutes.to_place.emit(1.0)
        self.assertEqual(view.tab(), TRANSCRIPT)
        self.assertEqual(self.program.open_meeting(second).tab(), TRANSCRIPT)

    def test_minutes_and_transcript_toasts_open_on_the_minutes(self):
        """The toasts "Minutes ready" and "Transcript ready" open the meeting on the minutes, whatever tab was last,
        and the next meeting opens there too; the meeting open already stays on the tab it shows."""
        first, second, third = (self._meeting(f"2026-10-27_1{n}-00") for n in range(3))
        self.program.settings.open_in = "программе"
        self.program.open_meeting(first).show_tab(NOTES)
        self.program._on_message_answer(second / minutes.FILE, "нажато")
        self.assertEqual(self.program.window.meeting.tab(), MINUTES)
        self.assertEqual(self.program.open_meeting(third).tab(), MINUTES)
        self.program.window.meeting.show_tab(TRANSCRIPT)
        self.program._on_message_answer(first / transcript.TRANSCRIPT, "нажато")
        self.assertEqual(self.program.window.meeting.tab(), MINUTES)
        self.program.window.meeting.show_tab(TRANSCRIPT)
        self.program._on_message_answer(first / transcript.TRANSCRIPT, "нажато")  # "Transcribe again" is done
        self.assertEqual(self.program.window.meeting.tab(), TRANSCRIPT)

    def test_notes_from_the_project_page_open_on_the_notes(self):
        """Also the meeting open already goes to its notes, as before."""
        first, second = self._meeting("2026-10-27_10-00"), self._meeting("2026-10-27_11-00")
        self.program.window.open_notes.emit(first)
        self.assertEqual(self.program.window.meeting.tab(), NOTES)
        self.assertEqual(self.program.open_meeting(second).tab(), NOTES)
        self.program.window.meeting.show_tab(MINUTES)
        self.program.window.open_notes.emit(second)
        self.assertEqual(self.program.window.meeting.tab(), NOTES)


class TestSettingsWindow(unittest.TestCase):
    def test_changes_made_elsewhere_are_not_rolled_back(self):
        """The settings section sends its whole copy: a project renamed on the left and the collapsed recording bar
        stay as they are when a setting is changed in it afterwards."""
        program = program_for(self)
        program.settings.tips_shown = list(settings.ALL_TIPS)
        with mock.patch.object(program, "show_window"), mock.patch.object(program.window, "show_page") as shown:
            program.open_settings()
        page = shown.call_args.args[0]
        self.addCleanup(page.deleteLater)
        program.apply_settings(dataclasses.replace(program.settings, projects=["Остатки"]))
        program.session._bar_collapsed(True)
        page.changed.emit(dataclasses.replace(page.settings, theme="темная"))
        self.assertEqual(
            (program.settings.theme, program.settings.projects, program.settings.bar_collapsed),
            ("темная", ["Остатки"], True),
        )


class TestApplySettings(unittest.TestCase):
    def test_changing_theme_does_not_retry_unavailable_shortcuts(self):
        program = program_for(self)
        program.key_seq = mock.Mock(**{"enable.return_value": False})
        program.mark_hotkey = mock.Mock(**{"enable.return_value": False})
        program.notes_hotkey = mock.Mock(**{"enable.return_value": False})
        with mock.patch.object(program, "_apply_theme") as apply_theme:
            program.apply_settings(dataclasses.replace(program.settings, theme="светлая"))
        apply_theme.assert_called_once()
        for shortcut in (program.key_seq, program.mark_hotkey, program.notes_hotkey):
            shortcut.enable.assert_not_called()
        program.notify.assert_not_called()

    def test_refused_shortcuts_and_unsaved_settings_are_reported(self):
        """The settings window shows what was typed; what Windows and the disk did not take is said by the app."""
        program = program_for(self)
        program.mark_hotkey = mock.Mock(**{"enable.return_value": False})
        with mock.patch.object(app.settings, "save", side_effect=FULL), self.assertLogs(app.log, "WARNING"):
            program.apply_settings(dataclasses.replace(program.settings, mark_hotkey="Ctrl+Alt+R"))
        headings = [c.args[0] for c in program.notify.call_args_list]
        self.assertIn(app.tr("Settings not saved"), headings)
        self.assertIn(app.tr("Shortcut not available"), headings)
        self.assertEqual(program.settings.mark_hotkey, "Ctrl+Alt+R")  # the app goes on with the new settings


class TestHelpers(unittest.TestCase):
    def test_projects_and_recognition_params_in_one_place(self):
        program = program_for(self)
        folder = program.settings.recordings / "2026-10-27_10-00"
        folder.mkdir()
        (folder / "микрофон.flac").write_bytes(b"x")
        catalog.set_(folder, project="Склад")
        program.settings.projects = ["Остатки"]
        self.assertEqual(program._projects(), ["Остатки", "Склад"])
        program.settings.delete_audio_after = 0
        params, delete_after = program._recognition_params()
        self.assertIs(params.cancel, program.jobs.cancel)
        self.assertTrue(delete_after)


class TestQuit(unittest.TestCase):
    def setUp(self):
        self.program = program_for(self)
        self.program.server, self.program.lock = mock.Mock(), mock.Mock()
        self.folder = self.program.settings.recordings / "2026-10-27_10-00"
        self.folder.mkdir()
        patch = mock.patch.object(app, "QApplication")
        self.qt = patch.start()
        self.addCleanup(patch.stop)

    def _answer(self, answer: QMessageBox.StandardButton) -> mock._patch:
        return mock.patch.object(app.QMessageBox, "question", return_value=answer)

    def test_exit_during_transcription_asks_first(self):
        """Exiting while a meeting is transcribed or minutes are written asks, as during a recording."""
        self.program.busy_items[self.folder] = Busy.TRANSCRIBING
        with self._answer(QMessageBox.StandardButton.No) as asked:
            self.program.quit_app()
        asked.assert_called_once()
        self.qt.exit.assert_not_called()
        self.program.server.close.assert_not_called()
        with self._answer(QMessageBox.StandardButton.Yes):
            self.program.quit_app()
        self.qt.exit.assert_called_once()
        self.program.lock.unlock.assert_called_once()

    def test_exit_during_recording_saves_it_first(self):
        """The window does not freeze for a minute waiting for the recording: a toast says it is being saved, a new
        start no longer talks to this copy, and the lock is let go once the recording is saved."""
        self.program.session.recording = mock.Mock(folder=self.folder, duration=mock.Mock(return_value=5.0))
        with self._answer(QMessageBox.StandardButton.Yes):
            self.program.quit_app()
        self.program.server.close.assert_called_once()
        self.assertIn("Сохраняю запись", self.program.notify.call_args.args[0])
        self.qt.exit.assert_not_called()
        self.program.lock.unlock.assert_not_called()
        self.program.session._on_end(self.folder, "")
        self.program._notify_with_buttons.assert_not_called()  # no "Transcribe?" while exiting
        self.qt.exit.assert_called_once()
        self.program.lock.unlock.assert_called_once()

    def test_meeting_that_keeps_its_edit_keeps_the_app(self):
        """The open meeting can't save its edit and the user keeps it: the exit stops before anything is let go,
        the app stays whole and can be exited later."""
        with mock.patch.object(self.program.window, "close", return_value=False):
            self.program.quit_app()
        self.program.server.close.assert_not_called()
        self.program.lock.unlock.assert_not_called()
        self.qt.exit.assert_not_called()
        self.assertFalse(self.program.jobs.cancel.is_set())
        with mock.patch.object(self.program.window, "close", return_value=True):
            self.program.quit_app()
        self.qt.exit.assert_called_once()

    def test_exit_asked_once_at_a_time(self):
        """A second Exit or --quit while the question is open does not run the exit again."""
        self.program.session.recording = mock.Mock(folder=self.folder, duration=mock.Mock(return_value=5.0))

        def answer(*_arguments: object) -> QMessageBox.StandardButton:
            self.program.quit_app()
            return QMessageBox.StandardButton.Yes

        with mock.patch.object(app.QMessageBox, "question", side_effect=answer):
            self.program.quit_app()
        self.program.recording.stop.assert_called_once()

    def test_notes_that_cannot_be_saved_are_not_lost_by_the_exit(self):
        self.program.session.recording = mock.Mock(folder=self.folder, duration=mock.Mock(return_value=5.0))
        self.program.session._notepad = mock.Mock(folder=self.folder)
        self.program.session._notepad.notepad.save.return_value = False
        self.program.session._notepad.notepad.failure.text.return_value = failures.text(FULL)
        with mock.patch.object(
            app.QMessageBox, "question", side_effect=[QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No]
        ) as asked:
            self.program.quit_app()
        self.assertIn("Выйти и потерять их?", asked.call_args.args[2])
        self.program.recording.stop.assert_not_called()
        self.program.server.close.assert_not_called()

    def test_background_jobs_do_not_keep_the_process(self):
        self.assertEqual(self.program.wait_background(0), 0)
        gate = threading.Event()
        self.program.jobs.minutes_pool.start(gate.wait)
        self.addCleanup(gate.set)
        with mock.patch.object(app, "WAIT_BACKGROUND_S", 0.1), mock.patch.object(app.os, "_exit") as exited:
            self.program.wait_background(3)
        exited.assert_called_once_with(3)


class TestStart(unittest.TestCase):
    def test_failed_start_is_explained(self):
        """A start that fails says so in words, with the log to look at, instead of nothing at all."""
        hooks = (sys.excepthook, threading.excepthook)
        self.addCleanup(lambda: (setattr(sys, "excepthook", hooks[0]), setattr(threading, "excepthook", hooks[1])))
        with (
            mock.patch.object(app.journal, "setup"),
            mock.patch.object(app, "_translator"),
            mock.patch.object(app.style, "PopupHider"),
            mock.patch.object(app, "App", side_effect=PermissionError(13, "Access is denied", "настройки.json")),
            mock.patch.object(app.QMessageBox, "critical") as told,
            self.assertLogs(app.log, "ERROR"),
        ):
            self.assertEqual(app.run(mock.Mock(), mock.Mock(), ["knotes"]), 1)
        self.assertIn("Нет доступа", told.call_args.args[2])
        self.assertIn("журнал.log", told.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
