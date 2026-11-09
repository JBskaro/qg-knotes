# SPDX-License-Identifier: GPL-3.0-only
"""What the meeting list shows, read without holding up the window.

The meetings, the price of sending one and the open task count are read in a background thread; the minutes summary
of a row and the search matches of a meeting are read once per version of the file. A file that cannot be read (not
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
_matches: dict[pathlib.Path, tuple[tuple, int]] = {}  # folder -> (query and what was searched, matches)


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
            minutes_result(meeting.folder)
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


def minutes_result(folder: pathlib.Path) -> str:
    """minutes.result, read again only when the minutes file changed; unreadable minutes give no summary."""
    version = _version(folder / minutes.FILE)
    if version is None:
        return ""
    known = _summaries.get(folder)
    if known is not None and known[0] == version:
        return known[1]
    try:
        summary = minutes.result(folder)
    except (OSError, ValueError) as error:
        log.warning("minutes of %s not read: %s", folder.name, error)
        summary = ""
    _summaries[folder] = (version, summary)
    return summary


def find(meetings: list[catalog.Meeting], query: str) -> dict[pathlib.Path, int]:
    """catalog.find meeting by meeting: a transcript is read again only when it or the query changed; one that
    cannot be read is searched by its title and project."""
    result = {}
    for meeting in meetings:
        version = _version(meeting.transcript) if meeting.status == catalog.READY else None
        key = (query, meeting.display_title, meeting.project, meeting.status, version)
        known = _matches.get(meeting.folder)
        if known is None or known[0] != key:
            known = (key, _count(meeting, query))
            _matches[meeting.folder] = known
        if known[1]:
            result[meeting.folder] = known[1]
    return result


def _count(meeting: catalog.Meeting, query: str) -> int:
    try:
        return catalog.find([meeting], query).get(meeting.folder, 0)
    except (OSError, ValueError) as error:
        log.warning("transcript of %s not searched: %s", meeting.folder.name, error)
        by_title = dataclasses.replace(meeting, status=catalog.RECORDED)  # only what the list shows
        return catalog.find([by_title], query).get(meeting.folder, 0)
