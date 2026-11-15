# SPDX-License-Identifier: GPL-3.0-only
"""Background checks for releases and installation after confirmation. Active recordings and processing
are protected from restarts."""

import logging
import pathlib
import time
from collections.abc import Callable
from typing import NamedTuple

from PySide6.QtCore import QObject, QThreadPool, Signal

from knotes import VERSION, failures, providers, settings, updates
from knotes.language import tr
from knotes.ui.background import run_in_background

log = logging.getLogger(__name__)
CHECK_INTERVAL_S = 15 * 60
RETRY_INTERVAL_S = 2 * 60


class Prepared(NamedTuple):
    kind: str  # "установщик" or "копия"
    path: pathlib.Path


class Updates(QObject):
    """busy(): a recording, a recognition or the minutes are going on now."""

    notice = Signal(str, str)  # heading and text of a toast
    offered = Signal(object, bool)  # newer GitHub release and whether the user requested the check
    current = Signal()
    downloading = Signal(bool)
    started = Signal()  # the installer or the swap runs: the program exits for it

    def __init__(self, busy: Callable[[], bool]):
        super().__init__()
        self._busy = busy
        self._downloading = False
        self._next_check = 0.0
        self._checking = False
        self._manual_requested = False

    def check(self, config: settings.Settings, manual: bool = False) -> None:
        if not manual and (not config.check_for_updates or not updates.frozen() or time.monotonic() < self._next_check):
            return
        self._manual_requested |= manual
        if self._checking:
            return
        self._checking = True
        run_in_background(QThreadPool.globalInstance(), updates.latest, self._release_arrived)

    def _release_arrived(self, result: object) -> None:
        self._checking = False
        manual, self._manual_requested = self._manual_requested, False
        if isinstance(result, Exception):
            self._next_check = time.monotonic() + RETRY_INTERVAL_S
            log.warning("cannot check for updates: %s", result)
            if manual:
                self.notice.emit(tr("Cannot check for updates"), failures.text(result))
            return
        self._next_check = time.monotonic() + CHECK_INTERVAL_S
        if result is None:
            self.current.emit()
            if manual:
                self.notice.emit(
                    tr("Cannot check for updates"), tr("The program's releases on GitHub are not available.")
                )
            return
        if not updates.is_newer(result.version):
            self.current.emit()
            if manual:
                self.notice.emit(tr("No updates"), tr("You have the latest version {version}.", version=VERSION))
            return
        try:
            _installable_asset(result)
        except providers.ProviderError as error:
            self._next_check = time.monotonic() + RETRY_INTERVAL_S
            self.current.emit()
            if manual:
                self.notice.emit(tr("Cannot install the update"), str(error))
            return
        self.offered.emit(result, manual)

    def install(self, release: updates.Release) -> None:
        """Downloads the release file and installs it; not during a recording or a recognition."""
        if self._downloading:
            return
        if not updates.frozen():
            self.notice.emit(tr("Cannot install the update"), tr("The program is not running from the build."))
            return
        if self._busy():
            self.notice.emit(
                tr("I'll update later"),
                tr("A recording or processing is in progress. Click the update icon again when it finishes."),
            )
            return
        try:
            file = _installable_asset(release)
        except providers.ProviderError as error:
            self.notice.emit(tr("Cannot install the update"), str(error))
            return
        self._downloading = True
        self.downloading.emit(True)
        self.notice.emit(
            tr("Downloading the update"),
            tr("Version {version}. Once downloaded, the program restarts.", version=release.version),
        )
        run_in_background(QThreadPool.globalInstance(), lambda: _prepare(file), self._update_ready)

    def _update_ready(self, result: object) -> None:
        self._downloading = False
        self.downloading.emit(False)
        if isinstance(result, Exception):
            log.warning("the update did not download: %s", result)
            self.notice.emit(tr("The update did not download"), failures.text(result))
            return
        kind, path = result
        if self._busy():
            self.notice.emit(
                tr("I'll update later"),
                tr(
                    "Recording or processing started during the download. Click the update icon again when it finishes."
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


def _installable_asset(release: updates.Release) -> updates.Asset:
    file = updates.asset_for(release, updates.installed())
    if file is None:
        raise providers.ProviderError(
            tr("Release {version} has no file for this copy of the program.", version=release.version)
        )
    updates.verify_checksum(file)
    return file


def _prepare(file: updates.Asset) -> Prepared:
    path = updates.download(file)
    if updates.installed():
        return Prepared("установщик", path)
    return Prepared("копия", updates.stage_portable(path))
