# SPDX-License-Identifier: GPL-3.0-only
"""What the meeting list shows, read without holding up the window.

The meetings, the price of sending one and the open task count are read in a background thread; the minutes summary
of a row is read once per version of the file. A file that cannot be read (not
in UTF-8, cut off, locked by another program) costs its own row the detail, not the whole list.
"""

import dataclasses
import enum
import logging
import pathlib
import re

from knotes import catalog, minutes, tasks
from knotes.language import tr, tr_key

log = logging.getLogger(__name__)
_summaries: dict[pathlib.Path, tuple[tuple[int, int], str]] = {}  # folder -> (minutes file version, summary)


class Busy(enum.Enum):
    """What the program is doing with a meeting now."""

    RECORDING = tr_key("Идет запись")
    ASSEMBLING = tr_key("Собирается запись")
    TRANSCRIBING = tr_key("Распознается")
    RESTORING = tr_key("Восстанавливается")

    @property
    def caption(self) -> str:
        return tr(self.value)


@dataclasses.dataclass(frozen=True)
class Snapshot:
    meetings: list[catalog.Meeting]
    price: float | None  # of a second of audio; None when nothing is paid or there is nothing to estimate from
    open_tasks: int | None  # None: the board could not be read, the previous count stays


def read(recordings: pathlib.Path, priced: bool) -> Snapshot:
    """Everything the list shows, for a background thread; the minutes summaries are read here too, so the window
    thread finds them ready."""
    meetings = catalog.list_(recordings)
    for meeting in meetings:
        if meeting.status == catalog.READY:
            minutes_overview(meeting.folder)
    try:
        open_tasks = sum(z.opened for z in tasks.tasks(recordings, None, meetings))
    except (OSError, ValueError) as error:  # ValueError: minutes not in UTF-8
        log.warning("task board not read for the list: %s", error)
        open_tasks = None
    return Snapshot(meetings, catalog.price_per_second(meetings) if priced else None, open_tasks)


def search_pattern(query: str) -> re.Pattern:
    """The query as a pattern with one group that finds what catalog.find finds: any case, "е" and "ё" alike."""
    letters = "".join("[её]" if letter in "еёЕЁ" else re.escape(letter) for letter in query)
    return re.compile(f"({letters})", re.IGNORECASE)


def _version(path: pathlib.Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def minutes_overview(folder: pathlib.Path) -> tuple[str, bool]:
    """The result for a meeting row and whether a nonempty minutes file exists."""
    version = _version(folder / minutes.FILE)
    if version is None:
        return "", False
    known = _summaries.get(folder)
    if known is not None and known[0] == version:
        return known[1], bool(version[1])
    try:
        summary = minutes.result(folder)
    except (OSError, ValueError) as error:
        log.warning("minutes of %s not read: %s", folder.name, error)
        summary = ""
    _summaries[folder] = (version, summary)
    return summary, bool(version[1])
