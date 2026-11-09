# SPDX-License-Identifier: GPL-3.0-only
"""Background work on meetings and what the program is doing with each one now (meeting_list.Busy): recording,
assembling, transcribing, restoring; and the minutes being written.

Recognition takes one meeting at a time, so the network and the provider are not loaded with several at once; the
minutes do not wait while other meetings are recognized. Results come to the window thread by signals, the meeting
is no longer busy by then.
"""

import logging
import pathlib
import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal

from knotes import catalog, entry, failures, minutes, recognition, recording, screen, transcript
from knotes.meeting_list import Busy
from knotes.meeting_rows import Progress
from knotes.ui.background import run_in_background

log = logging.getLogger(__name__)
PROGRESS_MS = 1000  # recognition progress in the list: at most once a second


class Jobs(QObject):
    transcribed = Signal(object, object)  # folder, the transcript file or the error
    restored = Signal(object, bool, object)  # folder, whether its audio was assembled, what recording.collect gave
    minutes_written = Signal(object, object)  # folder, the minutes file or the error
    progress_shown = Signal(object, object)  # folder, Progress: at once, then at most once a second
    _progress_arrived = Signal(object, int, int)  # from the recognition thread

    def __init__(self):
        super().__init__()
        self.busy: dict[pathlib.Path, Busy] = {}
        self.minutes: set[pathlib.Path] = set()  # meetings whose minutes are being written now
        self.progress: dict[pathlib.Path, Progress] = {}
        self.cancel = threading.Event()  # set on exit: the local engine stops instead of holding up the exit
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(1)  # one meeting at a time: do not load the network and the provider
        self.minutes_pool = QThreadPool()  # minutes do not wait while other meetings are recognized
        self._progress_later = QTimer(self, singleShot=True, interval=PROGRESS_MS)
        self._progress_later.timeout.connect(self._show_progress)
        self._progress_changed: set[pathlib.Path] = set()
        self._progress_arrived.connect(self._on_progress)

    # --- speech recognition ---------------------------------------------------------------------------------

    def recognize(
        self, folder: pathlib.Path, params: recognition.Params, again: bool = False, delete_after: bool = False
    ) -> bool:
        """delete_after: the audio goes once the transcript is ready. False: the meeting is being transcribed
        already."""
        if self.busy.get(folder) is Busy.TRANSCRIBING:
            return False
        self.busy[folder] = Busy.TRANSCRIBING
        progress = self._progress_for(folder)
        run_in_background(
            self.pool,
            lambda: recognize_folder(folder, params, (again, delete_after), progress),
            lambda result: self._transcribed(folder, result),
        )
        return True

    def take_file(
        self, file: pathlib.Path, recordings: pathlib.Path, params: recognition.Params, delete_after: bool
    ) -> None:
        """A recording file becomes a meeting of the recordings folder and is transcribed."""
        folder = recognition.folder_for_file(file, recordings)
        self.busy[folder] = Busy.TRANSCRIBING
        progress = self._progress_for(folder)
        run_in_background(
            self.pool,
            lambda: recognize_folder(recognition.from_file(file, recordings), params, (False, delete_after), progress),
            lambda result: self._transcribed(folder, result),
        )

    def _transcribed(self, folder: pathlib.Path, result: object) -> None:
        """A failure stays in the meeting, so the list shows it with Retry."""
        self.busy.pop(folder, None)
        self.progress.pop(folder, None)
        if isinstance(result, Exception) and folder.exists():
            catalog.mark_error(folder, failures.text(result))
        self.transcribed.emit(folder, result)

    def restore(self, recordings: pathlib.Path) -> list[pathlib.Path]:
        """Recordings cut off by closing the program or by a reboot are assembled; returns the ones found."""
        found = recording.interrupted_all(recordings) if recordings.exists() else []
        for folder in found:
            self.busy[folder] = Busy.RESTORING
            audio = (folder / transcript.MANIFEST).exists()  # no audio manifest: only the video was not assembled
            run_in_background(
                self.pool,
                lambda p=folder: recording.collect(p),
                lambda result, p=folder, z=audio: self._restored(p, z, result),
            )
        return found

    def _restored(self, folder: pathlib.Path, audio: bool, result: object) -> None:
        self.busy.pop(folder, None)
        if isinstance(result, Exception):
            log.warning("interrupted recording %s was not assembled: %s", folder.name, result)
        self.restored.emit(folder, audio, result)

    def _progress_for(self, folder: pathlib.Path) -> Callable[[int, int], None]:
        return lambda done_count, total: self._progress_arrived.emit(folder, done_count, total)

    def _on_progress(self, folder: pathlib.Path, done_count: int, total: int) -> None:
        """Only the row of the meeting changes, at once and then at most once a second: a local engine reports every
        part, and rebuilding the whole list for each would stall the window."""
        old = self.progress.get(folder)
        start = old.start if old and done_count else time.monotonic()
        self.progress[folder] = Progress(done_count, total, start)
        self._progress_changed.add(folder)
        if not self._progress_later.isActive():
            self._show_progress()
            self._progress_later.start()

    def _show_progress(self) -> None:
        for folder in self._progress_changed & self.progress.keys():
            self.progress_shown.emit(folder, self.progress[folder])
        self._progress_changed.clear()

    # --- minutes --------------------------------------------------------------------------------------------

    def compose(self, folder: pathlib.Path, params: minutes.Params) -> None:
        self.minutes.add(folder)
        run_in_background(
            self.minutes_pool,
            lambda: minutes.compose(folder, params),
            lambda result: self._composed(folder, result),
        )

    def _composed(self, folder: pathlib.Path, result: object) -> None:
        self.minutes.discard(folder)
        self.minutes_written.emit(folder, result)


def recognize_folder(
    folder: pathlib.Path, params: recognition.Params, how: tuple[bool, bool], progress: Callable[[int, int], None]
) -> pathlib.Path:
    """how: (recognize again, delete the audio after)."""
    again, delete_after = how
    catalog.clear_error(folder)
    file = recognition.process(folder, params, again, progress, entry.payment(folder))
    if delete_after:
        for track in recognition.TRACKS:
            (folder / f"{track}.flac").unlink(missing_ok=True)
        (folder / screen.VIDEO_FILE).unlink(missing_ok=True)
    return file
