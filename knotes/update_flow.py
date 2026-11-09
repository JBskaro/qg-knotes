# SPDX-License-Identifier: GPL-3.0-only
"""Updates of the program: a check once a day by itself (only the build can update itself) or at once from the tray
menu, then the download and the hand-over to the installer or to the swap of the portable copy. Never during a
recording, a recognition or the minutes: those are not cut off by a restart."""

import datetime
import logging
import pathlib
from collections.abc import Callable
from typing import NamedTuple

from PySide6.QtCore import QObject, QThreadPool, Signal

from knotes import VERSION, failures, settings, updates
from knotes.language import tr
from knotes.ui.background import run_in_background

log = logging.getLogger(__name__)


class Prepared(NamedTuple):
    kind: str  # "установщик" or "копия"
    path: pathlib.Path


class Updates(QObject):
    """busy(): a recording, a recognition or the minutes are going on now."""

    notice = Signal(str, str)  # heading and text of a toast
    checked = Signal(str)  # the day of a check that got an answer: the check by itself waits for the next day
    offered = Signal(object, str)  # a newer updates.Release and its main point: to ask whether to install it
    started = Signal()  # the installer or the swap runs: the program exits for it

    def __init__(self, busy: Callable[[], bool]):
        super().__init__()
        self._busy = busy

    def check(self, config: settings.Settings, manual: bool = False) -> None:
        """Once a day by itself, if the settings allow it and this is the build; manual: from the tray menu, always,
        and a release that is not newer is reported too."""
        today = datetime.date.today().isoformat()
        if not manual and (not config.check_for_updates or not updates.frozen() or config.updates_checked == today):
            return
        run_in_background(
            QThreadPool.globalInstance(), updates.latest, lambda result: self._release_arrived(result, manual, today)
        )

    def _release_arrived(self, result: object, manual: bool, today: str) -> None:
        if isinstance(result, Exception):
            log.warning("cannot check for updates: %s", result)
            if manual:
                self.notice.emit(tr("Cannot check for updates"), failures.text(result))
            return
        self.checked.emit(today)
        if result is None:
            if manual:
                self.notice.emit(
                    tr("Cannot check for updates"), tr("The program's releases on GitHub are not available.")
                )
            return
        if not updates.is_newer(result.version):
            if manual:
                self.notice.emit(tr("No updates"), tr("You have the latest version {version}.", version=VERSION))
            return
        self.offered.emit(result, next((s.strip(" -*") for s in result.notes.splitlines() if s.strip(" -*")), ""))

    def install(self, release: updates.Release) -> None:
        """Downloads the release file and installs it; not during a recording or a recognition."""
        if not updates.frozen():
            self.notice.emit(tr("Cannot install the update"), tr("The program is not running from the build."))
            return
        if self._busy():
            self.notice.emit(
                tr("I'll update later"),
                tr(
                    'A recording or recognition is in progress. When it ends, use "Check for updates" at the icon by '
                    "the clock."
                ),
            )
            return
        file = updates.asset_for(release, updates.installed())
        if file is None:
            self.notice.emit(
                tr("Cannot install the update"),
                tr("Release {version} has no file for this copy of the program.", version=release.version),
            )
            return
        self.notice.emit(
            tr("Downloading the update"),
            tr("Version {version}. Once downloaded, the program restarts.", version=release.version),
        )
        run_in_background(QThreadPool.globalInstance(), lambda: _prepare(file), self._update_ready)

    def _update_ready(self, result: object) -> None:
        if isinstance(result, Exception):
            log.warning("the update did not download: %s", result)
            self.notice.emit(tr("The update did not download"), failures.text(result))
            return
        kind, path = result
        if self._busy():
            self.notice.emit(
                tr("I'll update later"),
                tr(
                    'A recording or recognition started while downloading. When it ends, use "Check for updates" at '
                    "the icon by the clock."
                ),
            )
            return
        try:
            if kind == "установщик":
                updates.run_setup(path)
            else:
                updates.start_swap(path)
        except OSError as error:
            log.warning("cannot start the update: %s", error)
            self.notice.emit(tr("Cannot install the update"), failures.text(error))
            return
        self.started.emit()


def _prepare(file: updates.Asset) -> Prepared:
    path = updates.download(file)
    if updates.installed():
        return Prepared("установщик", path)
    return Prepared("копия", updates.stage_portable(path))
