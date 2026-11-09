# SPDX-License-Identifier: GPL-3.0-only
"""Rows of the meeting list: what a row says about its meeting (state, progress, sending cost, search matches) and
which rows the selected project and the search leave in the list."""

import dataclasses
import pathlib
import time

from knotes import catalog, meeting_list
from knotes.language import tr_key, tr

STATES = {
    catalog.READY: (tr_key("Готова"), "готова"),
    catalog.RECORDED: (tr_key("Не отправлена"), "ждет"),
    catalog.FILE: (tr_key("Не распознан"), "ждет"),
    catalog.INTERRUPTED: (tr_key("Прервана"), "ждет"),
    catalog.RECOGNITION_ERROR: (tr_key("Ошибка"), "ошибка"),
}  # caption (on screen through tr()) and color
TRACK_COUNT = 2  # an own recording has two tracks, both are paid for


@dataclasses.dataclass(frozen=True)
class Progress:
    """How many audio parts are recognized and when it started, to show "2 of 3, about 1 min left"."""

    done_count: int
    total: int
    start: float

    def remaining(self, now: float | None = None) -> str:
        if not self.done_count or self.done_count >= self.total:
            return ""
        per_part = ((now or time.monotonic()) - self.start) / self.done_count
        return tr("about {n} min left", n=max(1, round(per_part * (self.total - self.done_count) / 60)))


@dataclasses.dataclass(frozen=True)
class MeetingItem:
    """What to show about a meeting: state, progress, sending cost, how many matches were found."""

    meeting: catalog.Meeting
    occupied: meeting_list.Busy | None = None
    progress: Progress | None = None
    price: float | None = None
    found: int = 0
    result: str = ""  # of the minutes: "3 decisions · 5 tasks"

    @property
    def status(self) -> tuple[str, str]:
        """Caption in the interface language and color."""
        if self.occupied:
            if self.progress and self.progress.total > 1:
                return tr(
                    "{status}, {done_count} of {total}",
                    status=self.occupied.caption,
                    done_count=self.progress.done_count,
                    total=self.progress.total,
                ), "идет"
            return self.occupied.caption, "идет"
        row_label, color = STATES.get(self.meeting.status, (self.meeting.status, "ждет"))
        return tr(row_label), color

    @property
    def recognizable(self) -> bool:
        return not self.occupied and self.meeting.has_audio and self.meeting.status != catalog.INTERRUPTED


def items(
    meetings: list[catalog.Meeting],
    busy_items: dict[pathlib.Path, meeting_list.Busy],
    progress: dict[pathlib.Path, Progress],
    price_per_second: float | None,
) -> list[MeetingItem]:
    """Rows of the meetings; for busy ones, what the program is doing with them now ("Recording", "Transcribing")."""
    return [
        MeetingItem(
            v,
            busy_items.get(v.folder),
            progress.get(v.folder),
            price_per_second * v.seconds * TRACK_COUNT if price_per_second and v.seconds else None,
            result=meeting_list.minutes_result(v.folder) if v.status == catalog.READY else "",
        )
        for v in meetings
    ]


def with_progress(rows: list[MeetingItem], folder: pathlib.Path, progress: Progress) -> list[MeetingItem]:
    """The same rows, the meeting of the folder with its new progress."""
    return [dataclasses.replace(v, progress=progress) if v.meeting.folder == folder else v for v in rows]


def of_project(rows: list[MeetingItem], project: str | None) -> list[MeetingItem]:
    """Rows of the project selected on the left: None - all, "" - no project."""
    return [v for v in rows if project is None or v.meeting.project == project]


def found_only(rows: list[MeetingItem], found: dict[pathlib.Path, int]) -> list[MeetingItem]:
    """Search results: only the meetings with matches, each with how many it has."""
    return [dataclasses.replace(v, found=found[v.meeting.folder]) for v in rows if v.meeting.folder in found]
