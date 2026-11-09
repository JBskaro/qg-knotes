# SPDX-License-Identifier: GPL-3.0-only
"""The recording session: start and stop, pause, marks, the meeting notepad over other windows, the live transcript,
the recording bar with its warnings (devices, silence) and the calendar event of the meeting.

The recording runs in its own threads (recording.Recording); its events come here by signals. The program shows what
the session reports: the tray icon, the record button, the toasts after the recording.
"""

import dataclasses
import logging
import pathlib
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, QPoint, QRect, QThreadPool, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMessageBox

from knotes import catalog, failures, ics_calendar, live, notes, recording, screen, settings, silence, tasks, transcript
from knotes.meeting_list import Busy
from knotes.ui import style
from knotes.ui.background import run_in_background
from knotes.ui.notepad import NotepadWindow
from knotes.ui.recording_bar import RecordingBar
from knotes.language import tr

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Host:
    """What the session takes from the program."""

    settings: Callable[[], settings.Settings]  # the current ones: the program replaces them with every change
    notify: Callable[..., None]  # heading, text and the file a click on the toast opens
    shortcuts: Callable[[], tuple[str, str, str]]  # the granted shortcuts: recording, mark, notepad
    projects: Callable[[], list[str]]
    notes_saved: Callable[[pathlib.Path], None]  # the meeting notes are on disk: views of them reload


class RecordingSession(QObject):
    """busy: what the program is doing with the meetings; the session sets "recording" and "assembling"."""

    running = Signal(bool)  # the recording started or ended
    ticked = Signal(float)  # seconds recorded, five times a second
    ended = Signal(object, str, float)  # folder, why it was interrupted (empty: saved), seconds; the state is reset
    changed = Signal()  # the recording stopped or got its calendar title: reread the list
    bar_changed = Signal()  # the bar folded or unfolded: the toasts under it move
    _changed_signal = Signal(str)
    _screen_stopped = Signal()
    _ended = Signal(object, str)

    def __init__(self, host: Host, busy: dict[pathlib.Path, Busy]):
        super().__init__()
        self._host, self._busy = host, busy
        self.recording: recording.Recording | None = None
        self.live: live.Live | None = None  # transcript during the recording, when it is on
        self.line: RecordingBar | None = None
        self.from_toast: set[str] = set()  # meeting programs whose toast started the recording
        self._silence = silence.Watch()
        self._notepad: NotepadWindow | None = None
        self._kept_notepads: list[NotepadWindow] = []  # notes the disk did not take, left open with their text
        self._save_place = QTimer(self, singleShot=True, interval=800)  # notepad place: saved on mouse release
        self._save_place.timeout.connect(lambda: settings.save(self._host.settings()))
        self._recorded_seconds = 0.0
        self._changed_signal.connect(self._on_change)
        self._screen_stopped.connect(lambda: self.line and self.line.show_screen_icon(False))
        self._ended.connect(self._on_end)

    def toggle(self) -> None:
        if self.recording:
            self.stop()
        else:
            self.start()

    def _what_to_record(self) -> screen.Capture | None:
        """What to record with the audio; None when screen recording is off or nothing to record was found."""
        config = self._host.settings()
        if not config.record_screen:
            return None
        try:
            capture = screen.choose_capture(config.screen_capture, config.meeting_apps, config.video_quality)
        except (OSError, IndexError) as error:
            log.warning("no monitor found for screen recording: %s", error)
            capture = None
        if capture is None:
            self._host.notify(
                tr("The screen is not recorded"),
                tr("The meeting program window or a monitor to record was not found. The audio is recorded as usual."),
            )
        return capture

    def start(self, program: str = "") -> None:
        """program: the meeting program whose toast started the recording; the meeting is named after it."""
        if self.recording:
            return
        config = self._host.settings()
        try:
            config.recordings.mkdir(parents=True, exist_ok=True)
            folder = recording.new_folder(config.recordings)
        except OSError as error:
            log.exception("cannot create the recording folder")
            self._host.notify(
                tr("Couldn't start recording"),
                tr("{reason} Recordings folder: {folder}.", reason=failures.text(error), folder=config.recordings),
            )
            return
        events = recording.Events(
            change_item=self._changed_signal.emit, ended=self._ended.emit, screen_stopped=self._screen_stopped.emit
        )
        self.recording = recording.Recording(folder, events, (config.microphone, config.output), self._what_to_record())
        self.recording.start_now()
        self.live = live.start(config, folder, self.recording.duration)
        if program:
            self.from_toast.add(program)
            self._set_meeting(
                folder, title=f"{settings.program_name(program)} {catalog.folder_date(folder):%d.%m %H:%M}"
            )
        self._busy[folder] = Busy.RECORDING
        self._show_bar(config)
        self._silence = silence.Watch()
        self.running.emit(True)
        if settings.key(ics_calendar.KEY):  # title and participants from the calendar, without delaying the start
            run_in_background(
                QThreadPool.globalInstance(),
                ics_calendar.meeting_now,
                lambda result, p=folder: self._calendar_event(p, result),
            )

    def _show_bar(self, config: settings.Settings) -> None:
        self.line = RecordingBar(config.hide_bar, *self._host.shortcuts())
        self.line.stop.connect(self.stop)
        self.line.mark.connect(self.mark)
        self.line.note.connect(self.note)
        self.line.pause.connect(self.pause)
        self.line.moved.connect(self._bar_moved)
        self.line.place_item(config.bar_position)
        self.line.show_screen_icon(self.recording.records_screen)
        self.line.appear()
        if config.bar_collapsed:
            self.line.collapse(True)
        self.line.collapsed.connect(self._bar_collapsed)

    def _calendar_event(self, folder: pathlib.Path, result: object) -> None:
        """Calendar event found: title, participants and series project; the open notepad gets the project agenda."""
        if isinstance(result, Exception):
            log.warning("calendar not read: %s", result)
            return
        if not isinstance(result, ics_calendar.Event) or not folder.is_dir():
            return
        ics_calendar.apply(folder, result, self._host.settings().recordings)
        project = catalog.properties(folder).get("проект", "")
        if (
            project and self._notepad is not None and self._notepad.folder == folder
        ):  # no project: leave the agenda alone
            self._notepad.select_project(project)
        self.changed.emit()

    def mark(self) -> None:
        """A "★ Mark" at this moment of the recording; without a recording, nothing."""
        if self.recording and self.line:
            seconds = self.recording.mark()
            self.line.warn("отметка", tr("★ Mark {time}", time=transcript.time(seconds)), 3)

    def note(self) -> None:
        """The meeting notepad over other windows: show or hide it. Without a recording, notes are in the meeting and on
        the project page."""
        if not (self.recording and self.line):
            self._host.notify(
                tr("The notepad opens while recording"),
                tr(
                    "Notes of past meetings are in the meeting, Notes tab. Questions for the next meeting are on the "
                    "project page."
                ),
            )
            return
        if self._notepad is not None and self._notepad.folder != self.recording.folder:  # from an interrupted recording
            self._close_notepad(self._notepad.folder)
        if self._notepad is None:
            self._notepad = self._new_notepad(self.recording)
        if self._notepad.isVisible() and self._notepad.isActiveWindow():
            self._notepad.hide_away()
        else:
            self._notepad.display()

    def _new_notepad(self, recording_: recording.Recording) -> NotepadWindow:
        """The recording notepad: at its last place or under the recording bar; a project meeting gets its agenda."""
        config = self._host.settings()
        window = NotepadWindow(
            recording_.folder,
            config.hide_bar,
            self._host.projects(),
            recording_.duration,
            self.live,
        )
        window.notepad.mark.connect(self.mark)
        window.place.connect(self._notepad_moved)
        place = config.notepad_place
        if place and any(
            e_.availableGeometry().contains(QPoint(place[0], place[1])) for e_ in QGuiApplication.screens()
        ):
            window.setGeometry(*place)
        elif self.line:
            window.setGeometry(self.line.x(), self.line.y() + self.line.height() + 6, max(self.line.width(), 520), 440)
        project = catalog.properties(recording_.folder).get("проект", "")
        if project and not window.notepad.agenda.items():
            window.notepad.set_agenda(tasks.agenda(config.recordings, project, recording_.folder))
        return window

    def _notepad_moved(self, place: QRect) -> None:
        self._host.settings().notepad_place = [place.x(), place.y(), place.width(), place.height()]
        self._save_place.start()

    def _close_notepad(self, folder: pathlib.Path) -> None:
        """Notes go to the file; discussed questions leave the project's next meeting agenda, the rest stay. Notes
        that could not be saved stay in the open notepad: the text is not lost."""
        if self._notepad is None:
            return
        if not self._notepad.notepad.save():  # not written: the notepad stays open with the text and the reason
            self._notepad.display()
            self._kept_notepads.append(self._notepad)  # the next recording gets a notepad of its own
            self._notepad = None
            return
        self._notepad.close()
        self._notepad.deleteLater()
        self._notepad = None
        try:
            if project := catalog.properties(folder).get("проект", ""):
                notes.close_agenda(self._host.settings().recordings, project, notes.read(folder))
            self._host.notes_saved(folder)
        except (OSError, ValueError) as error:  # ValueError: a notes file not in UTF-8
            log.warning("project agenda not updated after the meeting: %s", error)
            self._host.notify(tr("Project agenda not updated"), failures.text(error))

    def pause(self, on: bool) -> None:
        if self.recording and self.line:
            self.recording.pause(on)
            self.line.set_pause(on)

    def stop(self) -> None:
        """The recording stops first, then the notes are saved: a full disk must not keep it going."""
        if self.live:  # the engine process ends at once, before the recording is assembled
            self.live.stop()
        if self.line:
            self.line.close()
            self.line = None
        if self.recording:
            self._recorded_seconds = self.recording.duration()
            self._busy[self.recording.folder] = Busy.ASSEMBLING
            self.recording.stop()
            self._close_notepad(self.recording.folder)
        self.changed.emit()

    def _on_change(self, text: str) -> None:
        """Which devices are recorded, what changed or was not found: on the recording bar, each on its own line."""
        if self.line:
            self.line.warn(text, text, 10)

    def _on_end(self, folder: pathlib.Path, error: str) -> None:
        """The state is reset first: whatever fails after it, the icon is not left red and a new recording starts."""
        if self.live:  # also when the recording stopped by itself
            self.live.stop()
        self.live = None
        self.recording = None
        self.from_toast.clear()
        self._busy.pop(folder, None)
        self.running.emit(False)
        if self.line:  # the recording ended by itself (a device failed): its bar goes too
            self.line.close()
            self.line = None
        self._close_notepad(folder)  # also for an interrupted recording: the next recording gets its own notepad
        if not error:
            self._set_meeting(folder, seconds=round(self._recorded_seconds, 1))
        self.ended.emit(folder, error, self._recorded_seconds)

    def _set_meeting(self, folder: pathlib.Path, **fields: object) -> None:
        """Meeting properties written by the program itself: a disk problem is reported, the work goes on."""
        try:
            catalog.set_(folder, **fields)
        except OSError as error:
            log.warning("meeting properties not saved: %s", error)
            self._host.notify(tr("Couldn't save the meeting details"), failures.text(error))

    def tick(self) -> None:
        """Levels, time and devices on the bar and in the notepad; a track silent too long gets a warning."""
        if not self.recording or not self.line:
            return
        levels, seconds = self.recording.levels(), self.recording.duration()
        self.line.refresh(seconds, levels)
        if self._notepad is not None:
            self._notepad.refresh(seconds)
        self.line.label_devices(self.recording.devices())
        self.ticked.emit(seconds)
        if self.recording.paused:
            return
        for track, silent in self._silence.step(levels, time.monotonic()).items():
            if silent:
                microphone = self.recording.devices().get("микрофон", "")
                self.line.warn(track, silence.text(track, self.from_toast, microphone), expand=True)
            else:
                self.line.remove(track)

    def _bar_moved(self, point: QPoint) -> None:
        config = self._host.settings()
        config.bar_position = [point.x(), point.y()]
        settings.save(config)

    def _bar_collapsed(self, collapsed: bool) -> None:
        config = self._host.settings()
        if collapsed != config.bar_collapsed:
            config.bar_collapsed = collapsed
            settings.save(config)
        self.bar_changed.emit()

    def hide_from_capture(self, hide_: bool) -> None:
        """The bar and the notepads in screen sharing or not, as set."""
        for window in (self.line, self._notepad, *self._kept_notepads):
            if window is not None:
                window.hide_from_capture(hide_)

    def notes_may_go(self) -> bool:
        """Notes the disk does not take are not lost with the exit without asking."""
        for window in (w for w in (self._notepad, *self._kept_notepads) if w is not None):
            if not window.notepad.save():  # the notepad shows why under its text
                reason = window.notepad.failure.text()
                question = tr("{reason} The meeting notes can't be saved. Exit and lose them?", reason=reason)
                return QMessageBox.question(None, style.NAME, question) == QMessageBox.StandardButton.Yes
        return True
