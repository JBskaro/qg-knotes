# SPDX-License-Identifier: GPL-3.0-only
"""Reminder to record a calendar meeting: at its start the program asks «Meeting started, record?». Meetings are
often moved and cancelled, so it asks rather than records an empty room by itself. The calendar comes by the ICS link
from the settings and is read again every quarter of an hour, so a moved meeting is reminded of at its new time."""

import dataclasses
import datetime

from knotes import ics_calendar, settings

POLL_S = 30  # how often the program looks whether a meeting has started
READ_EVERY = datetime.timedelta(minutes=15)
LATE_MINUTES = 10  # a meeting that started longer ago (the computer slept through its start) is not reminded of
ALL_DAY = datetime.timedelta(hours=12)  # an event this long is a day off or a trip, not a meeting


@dataclasses.dataclass
class Reminder:
    """The calendar meetings read last and the ones already reminded of, by title and start."""

    events: list[ics_calendar.Event] = dataclasses.field(default_factory=list)
    read_at: datetime.datetime | None = None
    reminded: set[tuple[str, datetime.datetime]] = dataclasses.field(default_factory=set)

    def stale(self, now: datetime.datetime) -> bool:
        return self.read_at is None or now - self.read_at >= READ_EVERY

    def read(self, events: list[ics_calendar.Event], now: datetime.datetime) -> None:
        self.events, self.read_at = list(events), now

    def due(self, now: datetime.datetime, busy: bool) -> list[ics_calendar.Event]:
        """Meetings that started in the last minutes, each once; while recording (busy) none, and the ones that
        started meanwhile are not reminded of after the recording either."""
        late = datetime.timedelta(minutes=LATE_MINUTES)
        found = [
            s
            for s in self.events
            if s.start <= now < s.start + late and s.end - s.start < ALL_DAY and (s.title, s.start) not in self.reminded
        ]
        self.reminded.update((s.title, s.start) for s in found)
        return [] if busy else found


def link() -> str:
    """The calendar link from the Windows credential store; empty without one."""
    return settings.key(ics_calendar.KEY)
