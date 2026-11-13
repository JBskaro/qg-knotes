# SPDX-License-Identifier: GPL-3.0-only
"""The QG Knotes app (package knotes): tray icon, recording bar, meetings window, settings, tips.

The app ties the parts together: the recording session (ui.recording_session), the background jobs on meetings
(meeting_jobs), the updates (update_flow), the meetings window and the toasts. Recording and speech recognition run
in background threads; windows get their results through signals.
"""

import dataclasses
import datetime
import logging
import os
import pathlib
import time
import urllib.parse
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QLibraryInfo, QLocale, QLockFile, QObject, Qt, QThreadPool, QTimer, QTranslator, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from knotes import (
    calendar_reminder,
    detector,
    catalog,
    failures,
    ics_calendar,
    instance,
    journal,
    minutes,
    providers,
    transcript,
    system,
    language,
    meeting_jobs,
    meeting_list,
    settings_changes,
    update_flow,
    updates,
)
from knotes import settings
from knotes import recognition
from knotes.hotkey import Hotkey
from knotes.recording import Recording
from knotes.ui import tour
from knotes.ui import style
from knotes.ui.meetings_window import MeetingsWindow
from knotes.ui.background import run_in_background
from knotes.ui.toast import Toast
from knotes.ui.meeting_view import MINUTES, NOTES, TRANSCRIPT, MeetingView
from knotes.ui.recording_session import Host, RecordingSession
from knotes.language import tr
from knotes.meeting_list import Busy

if TYPE_CHECKING:  # the sections and the setup load when first opened: a start does not wait for them
    from knotes.ui.setup_wizard import SetupWizard

log = logging.getLogger(__name__)
WAIT_RECORDING_MS = 60_000  # on exit: how long the recording may take to be saved
WAIT_BACKGROUND_S = 5  # after the windows close: how long recognition and minutes may take to finish
CLEANUP_MS = 6 * 3600 * 1000  # how often to check whether old audio is due for deletion
WAIT_ANSWER_S = 300  # "Send for transcription?" stays longer than a usual toast
UPDATES_AFTER_MS = 15_000  # the first check waits until the program has started
UPDATES_EVERY_MS = 6 * 3600 * 1000  # the program may run for days; one check a day still holds
SCHEMES = {"светлая": Qt.ColorScheme.Light, "темная": Qt.ColorScheme.Dark}


class App(QObject):
    _error_arrived = Signal(object)  # an uncaught error, from any thread

    def __init__(self, on_autostart: bool):
        super().__init__()
        self.settings, self._settings_problem = settings.read()
        self.on_autostart = on_autostart
        self._toasts: list[Toast] = []
        self._last_error: tuple[str, float] | None = None
        self.detector = detector.Detector(self.settings.meeting_apps, system.custom_exe())
        self.reminder = calendar_reminder.Reminder()
        self._calendar_reading = False
        self.jobs = meeting_jobs.Jobs()
        self.list_pool = QThreadPool()  # the meeting list is read off the window thread, one reading at a time
        self.list_pool.setMaxThreadCount(1)
        self._reading, self._read_again = False, False
        self.window = MeetingsWindow()
        self.tray = QSystemTrayIcon(style.tray_icon(False))
        self.session = RecordingSession(
            Host(lambda: self.settings, self.notify, self._shortcuts, self._projects, self._notes_saved), self.jobs.busy
        )
        self.updates = update_flow.Updates(self._busy)
        self._available_update: tuple[updates.Release, str] | None = None
        self.key_seq = Hotkey(int(self.window.winId()), 1, self.session.toggle)
        self.mark_hotkey = Hotkey(int(self.window.winId()), 2, self.session.mark)
        self.notes_hotkey = Hotkey(int(self.window.winId()), 3, self.session.note)
        self._transcript_windows: dict[pathlib.Path, MeetingView] = {}
        self._meeting_tab = MINUTES  # the tab the open meeting shows: the next one opens on it, until the exit
        self._tips: tour.Tour | None = None  # tips of the meetings window while they are shown
        self._wizard: SetupWizard | None = None
        self.lock: QLockFile | None = None  # this copy is the running one; let go once it has exited
        self.server: instance.Server | None = None  # commands of the copies started later
        self._quitting = self._quit_done = self._asking = False
        self._set_up = False  # the setup was finished: recognition is chosen, the tips do not send to it
        self._connect_signals()

    @property
    def recording(self) -> Recording | None:
        """The recording going on now: the exit, the restart and the update wait for it."""
        return self.session.recording

    @property
    def busy_items(self) -> dict[pathlib.Path, Busy]:
        """What the program is doing with each meeting now."""
        return self.jobs.busy

    @property
    def minutes_list(self) -> set[pathlib.Path]:
        """Meetings whose minutes are being written now."""
        return self.jobs.minutes

    def _busy(self) -> bool:
        return bool(self.recording or self.busy_items or self.minutes_list)

    def _connect_signals(self) -> None:
        self._error_arrived.connect(self._show_error)
        journal.listen(self._error_arrived.emit)
        self._connect_work()
        self.window.start_recording.connect(self.session.toggle)
        self.window.transcribe.connect(self.transcribe_files)
        self.window.recognize.connect(self.recognize_selected)
        self.window.open_meeting.connect(self.open_meeting)
        self.window.config.connect(self.open_settings)
        self.window.help_page.connect(self.open_help)
        self.window.summary.connect(self.open_summary)
        self.window.tasks.connect(self.open_tasks)
        self.window.open_notes.connect(self._open_notes)
        self.window.notice.connect(self.notify)
        self.window.projects_changed.connect(
            lambda projects: self.apply_settings(dataclasses.replace(self.settings, projects=projects))
        )
        self.window.changed.connect(lambda: self._refresh_window(always=True))
        self.window.in_sight.connect(lambda: self._refresh_window(always=True))
        self.window.project_looks_changed.connect(
            lambda kind: self.apply_settings(dataclasses.replace(self.settings, project_looks=kind))
        )
        self.window.layout_changed.connect(self._save_layout)
        self.window.set_layout(self.settings.window_layout)
        self.tray.activated.connect(self._tray_activated)
        self.tray.setContextMenu(self._menu())
        self._timers = [
            self._timer(200, self.session.tick),
            self._timer(detector.POLL_S * 1000, self._poll_detector),
            self._timer(calendar_reminder.POLL_S * 1000, self._poll_calendar),
            self._timer(5000, self._refresh_window),
            self._timer(CLEANUP_MS, self._clear_audio),
        ]

    def _connect_work(self) -> None:
        """The recording, the background jobs and the updates report here; the windows and the toasts show it."""
        self.session.running.connect(self._recording_shown)
        self.session.ticked.connect(self._recording_ticked)
        self.session.ended.connect(self._recording_ended)
        self.session.changed.connect(self._refresh_window)
        self.session.bar_changed.connect(self._arrange)
        self.jobs.transcribed.connect(self._recognition_result)
        self.jobs.restored.connect(self._restored)
        self.jobs.minutes_written.connect(self._minutes_result)
        self.jobs.progress_shown.connect(self.window.show_progress)
        self.updates.notice.connect(self.notify)
        self.updates.checked.connect(self._updates_checked)
        self.updates.offered.connect(self._offer_update)
        self.updates.current.connect(self._clear_update)
        self.window.update_requested.connect(self._confirm_update)
        self.updates.downloading.connect(lambda active: self.window.update_button.setEnabled(not active))
        self.updates.started.connect(self.quit_app)

    def _timer(self, ms: int, action: object) -> QTimer:
        timer = QTimer(self)
        timer.timeout.connect(action)
        timer.start(ms)
        return timer

    def _menu(self) -> QMenu:
        menu = QMenu()
        self._record_action = menu.addAction(tr("Start recording"), self.session.toggle)
        font = self._record_action.font()
        font.setBold(True)
        self._record_action.setFont(font)
        menu.addSeparator()
        menu.addAction(tr("Заметки встречи"), self.session.note)
        menu.addAction(tr("Transcribe a file…"), self.window.pick_files)
        menu.addAction(tr("Список встреч"), self.show_window)
        menu.addAction(tr("Tasks"), lambda: self.open_tasks(None))
        menu.addAction(tr("Summary"), self.open_summary)
        menu.addAction(tr("Settings"), self.open_settings)
        menu.addAction(tr("Проверить обновления"), lambda: self.check_updates(manual=True))
        menu.addAction(tr("Help"), self.open_help)
        menu.addSeparator()
        menu.addAction(QAction(tr("Exit"), menu, triggered=self.quit_app))
        return menu

    def startup(self) -> None:
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._repaint_theme)
        self._apply_theme()
        self._repaint_theme()
        for key_seq in (self.key_seq, self.mark_hotkey, self.notes_hotkey):
            QApplication.instance().installNativeEventFilter(key_seq)
        hotkey_ok = self.key_seq.enable(self.settings.hotkey)
        self._enable_other_hotkeys(self.settings)
        self._clear_audio()
        self.tray.setToolTip(tr("{name} - not recording", name=style.NAME))
        self.tray.show()
        if not self.on_autostart:
            how = (
                tr(
                    'To start recording, press {key_seq} or choose "Start recording" from the icon next to the clock',
                    key_seq=self.settings.hotkey,
                )
                if hotkey_ok and self.settings.hotkey
                else tr('To start recording, choose "Start recording" from the icon next to the clock')
            )
            self.notify(tr("{name} is running", name=style.NAME), how)
        if not hotkey_ok:
            self.notify(
                tr("Shortcut isn't working"),
                tr(
                    "Shortcut {key_seq} is used by another app. Choose a different one in Settings.",
                    key_seq=self.settings.hotkey,
                ),
            )
        if self._settings_problem:
            self.notify(tr("Settings reset"), self._settings_problem)
        self.restore_interrupted()
        if not self.on_autostart:
            self._first_run()
        if updates.frozen():
            updates.cleanup()
        QTimer.singleShot(UPDATES_AFTER_MS, self.check_updates)
        self._timers.append(self._timer(UPDATES_EVERY_MS, self.check_updates))

    # --- updates ------------------------------------------------------------------------------------------------

    def check_updates(self, manual: bool = False) -> None:
        """Once a day by itself (only the build can update itself), or manually from the tray or settings."""
        self.updates.check(self.settings, manual)

    def _updates_checked(self, today: str) -> None:
        if self.settings.updates_checked != today:
            self.settings = dataclasses.replace(self.settings, updates_checked=today)
            settings.save(self.settings)

    def _offer_update(self, release: updates.Release, main_point: str) -> None:
        self._available_update = (release, main_point)
        self.window.show_update(release.version)
        self._notify_with_buttons(
            tr("Version {version} is out", version=release.version),
            main_point,
            [("обновить", tr("Update")), ("позже", tr("Later"))],
            lambda answer: answer == "обновить" and self._install_available_update(),
            WAIT_ANSWER_S,
        )

    def _clear_update(self) -> None:
        self._available_update = None
        self.window.show_update("")

    def _confirm_update(self) -> None:
        if self._available_update is None:
            return
        release, main_point = self._available_update
        box = QMessageBox(self.window)
        box.setIcon(QMessageBox.Icon.Information)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setWindowTitle(tr("Version {version} is out", version=release.version))
        box.setText(tr("Install version {version} and restart the app?", version=release.version))
        if main_point:
            box.setInformativeText(main_point)
        install = box.addButton(tr("Update"), QMessageBox.ButtonRole.AcceptRole)
        later = box.addButton(tr("Later"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(later)
        box.exec()
        if box.clickedButton() is install and self._available_update == (release, main_point):
            self.updates.install(release)

    def _install_available_update(self) -> None:
        if self._available_update is not None:
            self.updates.install(self._available_update[0])

    # --- first run ----------------------------------------------------------------------------------------------

    def _first_run(self) -> None:
        """A new install opens the window with the setup, then its tips; each is shown once."""
        if "мастер" not in self.settings.tips_shown:
            self.show_window()
            QTimer.singleShot(0, self.show_wizard)
        elif "окно" not in self.settings.tips_shown:
            self.show_window()
            QTimer.singleShot(0, self.show_tips)

    def show_wizard(self) -> None:
        """The setup over the meetings window: sound, recognition, a test recording."""
        if self._wizard is not None:
            self._wizard.raise_()
            return
        from knotes.ui.setup_wizard import SetupWizard  # noqa: PLC0415 - loaded when first opened

        self._wizard = SetupWizard(self.settings, self.window)
        self._wizard.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._wizard.changed.connect(self._changes_of(self.settings))
        self._wizard.closed.connect(self._wizard_closed)
        self._wizard.open()

    def _wizard_closed(self, how: str) -> None:
        self._wizard = None
        self._set_up = self._set_up or how == "done"
        self._mark_tips("мастер")
        if "окно" not in self.settings.tips_shown:
            QTimer.singleShot(0, self.show_tips)

    def show_tips(self) -> None:
        """Tips of the meetings window; while nothing can recognize speech yet, the last one opens the settings."""
        if self._tips is not None:
            return
        steps = self.window.tips(
            self.settings.hotkey, self._set_up or recognition.done(recognition.params(self.settings))
        )
        self._tips = tour.Tour(self.window, steps)
        self._tips.finished.connect(self._tips_finished)
        self._tips.start()

    def _tips_finished(self, how: str) -> None:
        self._tips = None
        self._mark_tips("окно")
        if how == "action":
            self.open_settings()

    def _mark_tips(self, what: str) -> None:
        if what not in self.settings.tips_shown:
            self.settings = dataclasses.replace(self.settings, tips_shown=[*self.settings.tips_shown, what])
            settings.save(self.settings)

    def _tips_again(self) -> None:
        """From the help: the window tips now, the settings tips on the next opening of the settings."""
        self.settings = dataclasses.replace(
            self.settings, tips_shown=[p for p in self.settings.tips_shown if p not in ("окно", "настройки")]
        )
        settings.save(self.settings)
        self.window.go_back()
        QTimer.singleShot(0, self.show_tips)

    # --- recording ----------------------------------------------------------------------------------------------

    def _recording_shown(self, running: bool) -> None:
        """The tray icon and the record button follow the recording; once it ends, meeting prompts may come again."""
        if not running:
            self.detector.recording_ended(time.monotonic())
        self._show_recording(running)

    def _recording_ticked(self, seconds: float) -> None:
        self.window.set_recording(True, seconds)
        self.tray.setToolTip(tr("{name} - recording {time}", name=style.NAME, time=transcript.time(seconds)))

    def _recording_ended(self, folder: pathlib.Path, error: str, seconds: float) -> None:
        """The session has reset its state already: whatever fails here, a new recording starts."""
        if error:
            self.notify(
                tr("Recording interrupted"),
                tr("{error}. What was recorded is saved in folder {folder}.", error=error, folder=folder.name),
                folder,
            )
        elif not self._quitting:
            self._after_recording(folder, tr("Recording saved"), seconds)
        if self._quitting:
            self._finish_quit()
            return
        self._refresh_window()

    def _shortcuts(self) -> tuple[str, str, str]:
        """The shortcuts Windows granted, for the recording bar: recording, mark, notepad."""
        return self.key_seq.shortcut, self.mark_hotkey.shortcut, self.notes_hotkey.shortcut

    def _notes_saved(self, folder: pathlib.Path) -> None:
        """The meeting notes are on disk: the open meeting reloads its "Notes" tab, the project page its notes."""
        if window := self._transcript_windows.get(folder):
            window.notepad.reload_from_disk()
        self.window.refresh_project_page()

    def _after_recording(self, folder: pathlib.Path, heading: str, seconds: float = 0) -> None:
        """As set in the settings: ask whether to send for transcription, send at once, or only save."""
        if self.settings.after_recording == "сразу":
            self.recognize(folder)
        elif self.settings.after_recording == "спросить":
            provider = recognition.params(self.settings).provider.title
            how_many = f", {catalog.duration(seconds)}" if seconds else ""
            meeting = catalog.display_title(folder)
            question = (
                tr("{meeting}{how_many}. Transcribe on this computer?", meeting=meeting, how_many=how_many)
                if self._on_computer()
                else tr(
                    "{meeting}{how_many}. Send it to {provider} for transcription?",
                    meeting=meeting,
                    how_many=how_many,
                    provider=provider,
                )
            )
            self._notify_with_buttons(
                heading,
                question + self._price(seconds, 2),
                [("да", tr("Расшифровать")), ("нет", tr("Not now"))],
                lambda answer: self._on_send_answer(folder, answer),
                WAIT_ANSWER_S,
            )
        else:
            self.notify(
                heading,
                tr("{meeting}. You can transcribe it from the meeting list.", meeting=catalog.display_title(folder)),
                folder,
            )

    def _on_computer(self) -> bool:
        """Speech recognition on this computer is chosen (no price)."""
        return providers.connection("распознавание", self.settings.provider, self.settings.address).local_engine

    def _price(self, seconds: float, tracks_count: int) -> str:
        """Text " About 0,14 $." by the average price of past recognitions; empty with nothing to estimate from."""
        price = (
            catalog.price_per_second(catalog.list_(self.settings.recordings))
            if seconds and not self._on_computer()
            else None
        )
        if not price:
            return ""
        amount = price * seconds * tracks_count
        return " " + (tr("Under $0.01.") if amount < 0.01 else tr("About {amount}.", amount=catalog.money(amount)))

    def _on_send_answer(self, folder: pathlib.Path, answer: str) -> None:
        if answer == "да":
            self.recognize(folder)

    def _show_recording(self, running: bool) -> None:
        self.tray.setIcon(style.tray_icon(running))
        self._record_action.setText(tr("Stop recording") if running else tr("Start recording"))
        self.window.set_recording(running)
        if not running:
            self.tray.setToolTip(tr("{name} - not recording", name=style.NAME))

    # --- meeting prompts ----------------------------------------------------------------------------------------

    def _poll_detector(self) -> None:
        if not self.settings.suggest_recording:
            return
        step = self.detector.step(detector.read(self.detector.apps), time.monotonic(), self.recording is not None)
        for program in step.suggest:
            name = settings.program_name(program)
            self._notify_with_buttons(
                tr("Looks like a meeting has started"),
                tr("{program} turned on the microphone. Record?", program=name),
                [
                    ("записать", tr("Record")),
                    ("нет", tr("Not now")),
                    ("никогда", tr("Don't suggest for {program}", program=name)),
                ],
                lambda answer, p=program: self._on_meeting_answer(p, answer),
            )
        for program in step.finished_items & self.session.from_toast:
            text = tr("{program} released the microphone. Stop recording?", program=settings.program_name(program))
            self._notify_with_buttons(
                tr("Meeting over?"),
                text,
                [("стоп", tr("Stop recording")), ("нет", tr("Continue"))],
                self._on_end_answer,
            )

    def _notify_with_buttons(
        self, heading: str, text: str, buttons: list[tuple[str, str]], on_answer: object, fades_after: int = 20
    ) -> None:
        """A toast at the top of the screen under the recording bar; several toasts stack one under another."""
        tooltip = Toast(heading, text, buttons, self.settings.hide_bar, fades_after)
        tooltip.choice.connect(on_answer)
        tooltip.choice.connect(lambda _answer: self._remove_toast(tooltip))
        self._toasts.append(tooltip)
        self._arrange()
        tooltip.appear()

    def _remove_toast(self, tooltip: Toast) -> None:
        self._toasts.remove(tooltip)
        self._arrange()

    def _arrange(self) -> None:
        line = self.session.line
        below = line.height() + 8 if line and line.isVisible() else 0
        for tooltip in self._toasts:
            tooltip.top_center(below)
            below += tooltip.height() + 8

    def _now(self) -> datetime.datetime:
        return datetime.datetime.now()

    def _poll_calendar(self) -> None:
        """At the start of a calendar meeting: «Meeting started, record?»; the calendar is read in the background."""
        url = calendar_reminder.link() if self.settings.suggest_recording else ""
        if not url:
            return
        now = self._now()
        if self.reminder.stale(now) and not self._calendar_reading:
            self._calendar_reading = True
            run_in_background(
                QThreadPool.globalInstance(),
                lambda: ics_calendar.upcoming(url, now, 1),
                lambda result, moment=now: self._calendar_read(result, moment),
            )
        for event in self.reminder.due(now, self.recording is not None)[:1]:
            self._notify_with_buttons(
                tr("Meeting started"),
                tr("“{title}” on the calendar. Record?", title=event.title),
                [("записать", tr("Record")), ("нет", tr("Not now"))],
                self._on_calendar_answer,
            )

    def _calendar_read(self, result: object, moment: datetime.datetime) -> None:
        """A calendar that was not read (no network) keeps the meetings read before; tried again in 15 minutes."""
        self._calendar_reading = False
        if isinstance(result, Exception):
            log.warning("calendar not read for the reminder: %s", result)
        self.reminder.read(result if isinstance(result, list) else self.reminder.events, moment)

    def _on_calendar_answer(self, answer: str) -> None:
        """The title and participants come from the calendar when the recording starts."""
        if answer == "записать":
            self.session.start()

    def _on_meeting_answer(self, program: str, answer: str) -> None:
        if answer == "записать":
            self.session.start(program)
        elif answer == "никогда":
            others_ = [p for p in self.settings.meeting_apps if p.lower() != program.lower()]
            self.apply_settings(dataclasses.replace(self.settings, meeting_apps=others_))

    def _on_end_answer(self, answer: str) -> None:
        if answer == "стоп":
            self.session.stop()

    # --- speech recognition -------------------------------------------------------------------------------------

    def recognize(self, folder: pathlib.Path, again: bool = False) -> None:
        params, delete_after = self._recognition_params()
        if not self.jobs.recognize(folder, params, again, delete_after):
            return
        self._refresh_window()
        heading = tr("Transcribing on this computer") if params.provider.local_engine else tr("Sent for transcription")
        self.notify(
            heading, tr("{meeting}. You'll be notified when it's ready.", meeting=catalog.display_title(folder))
        )

    def _recognition_params(self) -> tuple[recognition.Params, bool]:
        """Recognition as set in the settings, stopped by the exit; and whether the audio is deleted right after."""
        params = dataclasses.replace(recognition.params(self.settings), cancel=self.jobs.cancel)
        return params, self.settings.delete_audio_after == 0

    def _projects(self) -> list[str]:
        """Projects of the meetings and the ones created in advance."""
        return catalog.projects(catalog.list_(self.settings.recordings), self.settings.projects)

    def recognize_selected(self, paths: list[pathlib.Path]) -> None:
        """From the meetings window: new ones at once, files are taken in, transcribed ones again after asking."""
        files = [p for p in paths if p.is_file()]
        if files:
            self.transcribe_files(files)
        folders = [p for p in paths if p.is_dir()]
        ready_items = [p for p in folders if (p / transcript.TRANSCRIPT).exists()]
        if ready_items and not self._confirm_again(ready_items):
            folders = [p for p in folders if p not in ready_items]
        for folder in folders:
            self.recognize(folder, again=folder in ready_items)

    def _confirm_again(self, folders: list[pathlib.Path]) -> bool:
        params = recognition.params(self.settings)
        seconds = sum((transcript.phrases(p) or {}).get("сведения", {}).get("длительность", 0) for p in folders)
        where = {"provider": params.provider.title, "model": params.final_model}
        question = (
            tr(
                'Transcribe "{meeting}" again with {provider}, {model}?',
                meeting=catalog.display_title(folders[0]),
                **where,
            )
            if len(folders) == 1
            else tr("Transcribe {n} meetings again with {provider}, {model}?", n=len(folders), **where)
        )
        answer = QMessageBox.question(
            self.window if self.window.isVisible() else None,
            tr("Transcribe again"),
            question
            + " "
            + tr("The current transcript and your text edits will be replaced; participant names and marks are kept.")
            + self._price(seconds, 2),
        )
        return answer == QMessageBox.StandardButton.Yes

    def transcribe_files(self, files: list[pathlib.Path]) -> None:
        params, delete_after = self._recognition_params()
        for file in files:
            self.jobs.take_file(file, self.settings.recordings, params, delete_after)
        self._refresh_window()

    def _recognition_result(self, folder: pathlib.Path, result: object) -> None:
        if isinstance(result, Exception):
            self.notify(
                tr("Couldn't transcribe the meeting"),
                tr(
                    "{meeting}. {reason} The recording is saved: you can transcribe it with Retry in the meeting list.",
                    meeting=catalog.display_title(folder),
                    reason=failures.text(result),
                ),
                folder,
            )
        else:
            self.notify(
                tr("Transcript ready"),
                tr("{meeting}. Click to open.", meeting=catalog.display_title(folder)),
                result,
            )
            if folder in self._transcript_windows:
                self._transcript_windows[folder].refresh()
        self._refresh_window()

    def restore_interrupted(self) -> None:
        """Recordings cut off by closing the program or by a reboot are assembled and recognized."""
        interrupted_all = self.jobs.restore(self.settings.recordings)
        if interrupted_all:
            self.notify(
                tr("Interrupted recording found"),
                tr("Assembling what was recorded: {folders}", folders=", ".join(p.name for p in interrupted_all)),
            )

    def _restored(self, folder: pathlib.Path, audio: bool, result: object) -> None:
        """Audio of an interrupted recording goes on as after a normal one; a video-only retry just reports."""
        if isinstance(result, Exception):
            self.notify(tr("The interrupted recording was not assembled"), failures.text(result))
        elif result:
            self.notify(tr("The video was not assembled"), str(result))
        if audio and not isinstance(result, Exception):
            self._after_recording(folder, tr("Interrupted recording restored"))
        self._refresh_window()

    # --- windows and settings -----------------------------------------------------------------------------------

    def show_window(self) -> None:
        self._refresh_window(always=True)
        system.bring_to_front(self.window)

    def open_meeting(self, folder: pathlib.Path, tab: str = "") -> MeetingView | None:
        """The meeting opens in the meetings window right of the list; only one can be open. A meeting opened anew is
        on the tab asked for, otherwise on the one the meeting before showed; the meeting open already stays on its
        tab. None: the open one keeps an edit it could not save, it stays."""
        window = self._transcript_windows.get(folder)
        if window is None:
            window = MeetingView(
                folder,
                self.settings.open_in == "блокноте",
                self._projects(),
                self._minutes_destination,
                tab or self._meeting_tab,
            )
            window.tab_shown.connect(self._tab_shown)  # after opening: the closest tab it gave way to is not remembered
            window.recognize.connect(lambda p: self.recognize_selected([p]))
            window.meeting_changed.connect(lambda: self._refresh_window(always=True))
            window.compose_minutes.connect(self.compose_minutes)
            window.ask_assistant.connect(self.ask_assistant)
            window.closed.connect(lambda w=window: self._forget_meeting(folder, w))
            window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            if not self.window.show_meeting(window, catalog.display_title(folder)):
                window.deleteLater()
                self.show_window()
                return None
            self._transcript_windows[folder] = window
            if tab:
                self._meeting_tab = window.tab()
            if folder in self.minutes_list:
                window.minutes_in_progress(True)
        self.show_window()
        return window

    def _tab_shown(self, tab: str) -> None:
        self._meeting_tab = tab

    def _open_notes(self, folder: pathlib.Path) -> None:
        """From the project page: the meeting on its "Notes" tab, also the one open already."""
        if (view := self.open_meeting(folder, NOTES)) is not None:
            view.show_tab(NOTES)

    def _forget_meeting(self, folder: pathlib.Path, window: object) -> None:
        """A closed meeting is no longer open, so a click opens it again; the same meeting opened anew stays."""
        if self._transcript_windows.get(folder) is window:
            del self._transcript_windows[folder]

    # --- minutes ------------------------------------------------------------------------------------------------

    def _minutes_destination(self) -> str:
        """The caption at the "Write minutes" button: where the transcript will go."""
        p = minutes.params(self.settings)
        model, server = providers.model_title(p.model), urllib.parse.urlsplit(p.service.address).netloc
        where_to = (
            tr("The transcript will be sent to {service}, {model}.", service=p.service.title, model=model)
            if p.service.key_needed
            else tr("The transcript will be sent to your server {server}, model {model}.", server=server, model=model)
        )
        name = (
            ""
            if self.settings.my_name
            else " " + tr("Свое имя вместо «Я» можно вписать в настройках, раздел «Протоколы».")
        )
        return where_to + name

    def compose_minutes(self, folder: pathlib.Path) -> None:
        if folder in self.minutes_list:
            return
        self.jobs.compose(folder, minutes.params(self.settings))
        if folder in self._transcript_windows:
            self._transcript_windows[folder].minutes_in_progress(True)

    def ask_assistant(self, folder: pathlib.Path) -> None:
        """The minutes are written by the user's AI assistant: the request goes to the clipboard."""
        QApplication.clipboard().setText(minutes.assistant_request(folder))
        window = self._transcript_windows.get(folder)
        if window:
            window.minutes.assistant_asked()
        else:
            self.notify(
                tr("Request copied"),
                tr("Paste it to your AI assistant: the minutes will appear in the meeting by themselves."),
            )

    def _minutes_result(self, folder: pathlib.Path, result: object) -> None:
        window = self._transcript_windows.get(folder)
        error = failures.text(result) if isinstance(result, Exception) else ""
        if window:
            window.minutes_in_progress(False, error)
        elif error:
            self.notify(tr("Couldn't write the minutes"), f"{catalog.display_title(folder)}. {error}")
        else:
            self.notify(
                tr("Minutes ready"),
                tr("{meeting}. Click to open.", meeting=catalog.display_title(folder)),
                result,
            )
        self._refresh_window()

    def open_help(self) -> None:
        """Help as its own section in the meetings window; a link in an answer opens its settings section."""
        from knotes.ui import help_page  # noqa: PLC0415 - loaded when first opened

        page = help_page.HelpPage()
        page.tips_again.connect(self._tips_again)
        page.check_sound.connect(self.show_wizard)
        page.open_settings.connect(self.open_settings_section)
        self.window.show_page(page, tr("Help"), "справка")
        self.show_window()

    def _clear_audio(self) -> None:
        if self.settings.delete_audio_after > 0:
            cleaned = catalog.clear_audio(catalog.list_(self.settings.recordings), self.settings.delete_audio_after)
            if cleaned:
                log.info("deleted old meeting audio: %s", ", ".join(p.name for p in cleaned))

    def _refresh_window(self, always: bool = False) -> None:
        """The list is read in the background, by the timer only while it is in sight; a refresh asked for during
        a reading comes after it."""
        if not (always or self.window.list_in_sight()):
            return
        if self._reading:
            self._read_again = True
            return
        self._reading = True
        recordings, priced = self.settings.recordings, not self._on_computer()
        run_in_background(self.list_pool, lambda: meeting_list.read(recordings, priced), self._list_read)

    def _list_read(self, result: object) -> None:
        self._reading = False
        if isinstance(result, Exception):
            self._show_error(result)
        else:
            self.window.set_projects(self.settings.projects, self.settings.project_looks)
            self.window.recordings_folder = self.settings.recordings
            self.window.show_meetings(result.meetings, self.busy_items, self.jobs.progress, result.price)
            if result.open_tasks is not None:
                self.window.set_tasks(result.open_tasks)
        if self._read_again:
            self._read_again = False
            self._refresh_window(always=True)

    def _save_layout(self, window_layout: dict) -> None:
        self.settings = dataclasses.replace(self.settings, window_layout=window_layout)
        settings.save(self.settings)

    def open_settings(self) -> None:
        """Settings as a section in the meetings window; changes apply at once."""
        if self._tips is not None:  # the window tips point at the window behind the settings
            self._tips.finish("closed")
        from knotes.ui.settings_window import SettingsWindow  # noqa: PLC0415 - loaded when first opened

        config = SettingsWindow(self.settings)
        config.changed.connect(self._changes_of(self.settings))
        config.restart.connect(self.restart)
        config.check_updates.connect(lambda: self.check_updates(manual=True))
        self.window.show_page(config, tr("Settings"), "настройки")
        self.show_window()
        if "настройки" not in self.settings.tips_shown:
            QTimer.singleShot(0, config, config.show_tips)

    def open_settings_section(self, code: str) -> None:
        self.open_settings()
        if hasattr(self.window.page, "show_section"):
            self.window.page.show_section(code)

    def open_summary(self) -> None:
        """The summary in the meetings window instead of the list, for the project selected on the left; the API key
        balance is fetched again."""
        from knotes.ui.summary_window import SummaryWindow  # noqa: PLC0415 - loaded when first opened

        params = recognition.params(self.settings)
        summary = SummaryWindow(
            self.settings.recordings,
            None if self._on_computer() else lambda: providers.balance(params.provider, params.key),
            params.provider.title,
            (self.settings.projects, self.settings.project_looks),
            self.window.project if self.window.isVisible() else None,
        )
        summary.open_meeting.connect(self.open_meeting)
        self.window.show_page(summary, tr("Summary"), "сводка")
        summary.display()
        summary.refresh_balance()
        self.show_window()

    def open_tasks(self, project: str | None) -> None:
        """The task board and project notes in the meetings window instead of the list."""
        from knotes.ui.tasks_window import TasksWindow  # noqa: PLC0415 - loaded when first opened

        meetings = catalog.list_(self.settings.recordings)
        page = TasksWindow(
            self.settings.recordings,
            (
                catalog.projects(meetings, self.settings.projects),
                self.settings.project_looks,
            ),
            project,
            meetings=meetings,
        )
        page.open_place.connect(self.open_place)
        self.window.show_page(page, tr("Tasks"), "задачи")
        self.show_window()

    def open_place(self, folder: pathlib.Path, second: float) -> None:
        """The meeting a task comes from, opened right on the transcript at that place."""
        if folder.is_dir() and (view := self.open_meeting(folder, TRANSCRIPT)) is not None:
            view.show_place(second)

    def _changes_of(self, start: settings.Settings) -> Callable[[settings.Settings], None]:
        """For a window with its own copy of the settings: only what it changed is applied."""
        edits = settings_changes.Edits(start)
        return lambda sent: self.apply_settings(edits.apply(self.settings, sent))

    def apply_settings(self, new_items: settings.Settings) -> None:
        previous = self.settings
        self.settings = new_items
        try:
            settings.save(new_items)
        except OSError as error:  # the app goes on with them; they are written again with the next change
            log.warning("settings not saved: %s", error)
            self.notify(tr("Settings not saved"), failures.text(error))
        self.detector.apps = new_items.meeting_apps
        self._enable_other_hotkeys(new_items, previous)
        if new_items.hotkey != previous.hotkey and not self.key_seq.enable(new_items.hotkey):
            self._shortcut_taken(new_items.hotkey)
        self._apply_theme()
        self.session.hide_from_capture(new_items.hide_bar)
        if new_items.recordings_folder != previous.recordings_folder:
            self._refresh_window()

    def _enable_other_hotkeys(self, config: settings.Settings, previous: settings.Settings | None = None) -> None:
        """Register mark and notepad shortcuts on startup or when their values change."""
        for field, hotkey in (("mark_hotkey", self.mark_hotkey), ("notes_hotkey", self.notes_hotkey)):
            shortcut = getattr(config, field)
            if previous is not None and shortcut == getattr(previous, field):
                continue
            if not hotkey.enable(shortcut):
                self._shortcut_taken(shortcut)

    def _shortcut_taken(self, shortcut: str) -> None:
        self.notify(
            tr("Shortcut not available"),
            tr("{key_seq} is used by another app, choose a different one.", key_seq=shortcut),
        )

    def _apply_theme(self) -> None:
        tips = QGuiApplication.styleHints()
        if self.settings.theme in SCHEMES:
            tips.setColorScheme(SCHEMES[self.settings.theme])
        else:
            tips.unsetColorScheme()

    def _repaint_theme(self) -> None:
        """The theme changed: the accent color and the windows' own colors follow it."""
        style.apply_accent(QApplication.instance())
        for window in QApplication.topLevelWidgets():
            if hasattr(window, "repaint_theme"):
                window.repaint_theme()

    # --- tray icon and notifications ----------------------------------------------------------------------------

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_window()

    def notify(self, heading: str, text: str, file: pathlib.Path | None = None) -> None:
        """A message as a toast at the top of the screen; a click opens the file (or the meetings window)."""
        self._notify_with_buttons(heading, text, [], lambda answer: self._on_message_answer(file, answer), 8)

    def _on_message_answer(self, file: pathlib.Path | None, answer: str) -> None:
        if answer != "нажато":
            return
        if file and file.name == transcript.TRANSCRIPT:
            self.open_transcript(file.parent)
        elif file and file.name == minutes.FILE:
            self.open_meeting(file.parent, MINUTES)
        elif file and file.exists():
            system.open_view(file)
        else:
            self.show_window()

    def open_transcript(self, folder: pathlib.Path) -> None:
        """As set: in the program window, in Windows Notepad or in the Windows program for .md; a meeting with minutes
        always opens in the program, right on the minutes."""
        if self.settings.open_in == "программе" or (folder / minutes.FILE).exists():
            self.open_meeting(folder, MINUTES)
        else:
            system.open_text(folder / transcript.TRANSCRIPT, self.settings.open_in == "блокноте")

    def _show_error(self, error: BaseException) -> None:
        """An uncaught error (logged by journal) as a toast, not silently: the exe has no console. The same error in
        a row (from a timer, say) is shown once a minute so as not to flood the screen."""
        text, now = failures.text(error), time.monotonic()
        if self._last_error and self._last_error[0] == text and now - self._last_error[1] < 60:
            return
        self._last_error = (text, now)
        self.notify(tr("Something went wrong"), text, journal.path())

    def restart(self) -> None:
        """The interface language changed: a new instance starts with the settings open, this one exits. A recording,
        a recognition or writing minutes is not interrupted: restart after they end."""
        if self.recording or self.busy_items or self.minutes_list:
            QMessageBox.information(
                None,
                style.NAME,
                tr("A recording, recognition or protocol is in progress. Restart the app when they finish."),
            )
            return
        system.launch_copy("--restart", "--settings")
        self.quit_app()

    # --- running copy and exit ---------------------------------------------------------------------------------

    def commands(self) -> dict[bytes, Callable[[], object]]:
        """What the copies started later may ask for."""
        return {
            instance.SHOW: self.show_window,
            instance.QUIT: self.quit_app,
            instance.TOGGLE: self.session.toggle,
            instance.SETTINGS: self.open_settings,
        }

    def take_instance(self, lock: QLockFile, server: instance.Server) -> None:
        self.lock, self.server = lock, server
        if not server.listening:
            self.notify(
                tr("The shortcut can't reach the app"),
                tr("A second click on the shortcut won't open this window. Restart the app if it keeps happening."),
            )

    def quit_app(self) -> None:
        """Asks first when a recording, a recognition or minutes would be cut off. The command server closes at once:
        a new start does not talk to the copy that is exiting; the lock is let go once the recording is saved, so the
        new copy does not assemble the same recording."""
        if self._quitting or self._asking:
            return
        self._asking = True  # a second Exit or --quit while the question is open is not a second exit
        try:
            if not self._may_quit():
                return
        finally:
            self._asking = False
        self._quitting = True
        if self.server is not None:
            self.server.close()
        if self.recording is None:
            self._finish_quit()
            return
        self.session.stop()
        self.notify(tr("Saving the recording"), tr("The app exits as soon as the recording is saved."))
        QTimer.singleShot(WAIT_RECORDING_MS, self._finish_quit)  # a recording that does not end does not keep the app

    def _may_quit(self) -> bool:
        """Everything that can stop the exit is asked before anything is let go: a recording, a recognition, notes
        and a meeting edit that can't be saved (the meetings window closes here, saving the open meeting)."""
        if self.recording:
            question = tr("Recording in progress. Stop it and exit?")
        elif self.busy_items or self.minutes_list:
            question = tr(
                "Transcription or minutes are in progress. Exit anyway? They stop; you can start them again from the "
                "meeting list."
            )
        else:
            question = ""
        if question and QMessageBox.question(None, style.NAME, question) != QMessageBox.StandardButton.Yes:
            return False
        return self.session.notes_may_go() and self.window.close()

    def _finish_quit(self) -> None:
        if self._quit_done:
            return
        self._quit_done = True
        self.key_seq.enable("")
        self.mark_hotkey.enable("")
        self.notes_hotkey.enable("")
        self.tray.hide()
        self.jobs.cancel.set()  # the local speech recognition engine stops instead of holding up the exit
        if self.lock is not None:
            self.lock.unlock()
        QApplication.exit(0)  # not quit(): it asks the windows again, and one refusing would leave the app half closed

    def wait_background(self, code: int) -> int:
        """Background jobs get a few seconds after the windows close; a provider that does not answer does not keep
        the process alive: it exits without them, the meeting can be sent again."""
        end = time.monotonic() + WAIT_BACKGROUND_S
        pools = (self.jobs.pool, self.jobs.minutes_pool, self.list_pool, QThreadPool.globalInstance())
        if all(pool.waitForDone(max(0, int((end - time.monotonic()) * 1000))) for pool in pools):
            return code
        log.warning("exiting without waiting for background jobs")
        logging.shutdown()
        os._exit(code)


def _translator(app: QApplication) -> QTranslator:
    """Sets the interface language before windows are created; standard Qt buttons are in Russian only in the
    Russian interface, calendars and months are in the interface language."""
    language.set_(settings.ui_language())
    QLocale.setDefault(QLocale(QLocale.Language.Russian if language.current() == "ru" else QLocale.Language.English))
    translator = QTranslator(app)
    if language.current() == "ru" and translator.load(
        "qtbase_ru", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    ):
        app.installTranslator(translator)
    return translator


def run(application: QApplication, lock: QLockFile, arguments: list[str]) -> int:
    """The first copy of the program: the log, uncaught errors, the windows. A start that fails is explained in
    words; after the windows close, background jobs get a few seconds to finish."""
    journal.setup()
    journal.catch_uncaught()
    application.setApplicationName(settings.NAME)
    application.setQuitOnLastWindowClosed(False)
    translator = _translator(application)
    application.setApplicationDisplayName(style.NAME)
    application.installEventFilter(style.PopupHider(application))  # notepad lists and tooltips stay out of sharing
    code = instance.command(arguments)
    try:
        program = App(on_autostart="--autostart" in arguments)
        program.take_instance(lock, instance.Server(program.commands()))
        program.startup()
    except Exception as error:  # noqa: BLE001 - the program cannot start: the user is told why, not left with nothing
        log.exception("the program did not start")
        QMessageBox.critical(
            None,
            style.NAME,
            tr("The app couldn't start. {reason}\n\nApp log: {file}", reason=failures.text(error), file=journal.path()),
        )
        return 1
    if code == instance.SETTINGS:
        QTimer.singleShot(0, program.open_settings)
    if code == instance.TOGGLE:  # the program was not running: start it and begin recording at once
        program.session.start()
    log.info("started, translations %s", translator.language())
    return program.wait_background(application.exec())
