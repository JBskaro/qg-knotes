# SPDX-License-Identifier: GPL-3.0-only
"""Calendar by an ICS link: the title and participants of the meeting that is going on now.

The new Outlook does not give programs its calendar on the computer, so the calendar is taken by a link: Outlook
("Publish calendar", ICS link), Google ("Secret address in iCal format"), Yandex ("Export"). The link is a secret,
it is kept in the Windows credential store like an API key. Recurring meetings are expanded: daily, weekly, monthly,
with an interval, weekdays, an end, a repeat count, exceptions and moved meetings.
"""

import contextlib
import dataclasses
import datetime
import logging
import pathlib
import re

from knotes import catalog, providers
from knotes import settings
from knotes.language import tr

log = logging.getLogger(__name__)
KEY = "календарь"  # link name in the Windows credential store
AHEAD = datetime.timedelta(minutes=15)  # a meeting that starts in 15 minutes counts as the current one
DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
REPEAT_LIMIT = 3000
RULE_PLAIN = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST"}


@dataclasses.dataclass(frozen=True)
class Event:
    title: str
    start: datetime.datetime  # local time without a time zone
    end: datetime.datetime
    participants: tuple[str, ...] = ()
    organizer: str = ""


def _lines(text: str) -> list[str]:
    """ICS lines with folded continuations joined (a line starting with a space continues the previous one)."""
    result: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and result:
            result[-1] += line[1:]
        elif line:
            result.append(line)
    return result


@dataclasses.dataclass(frozen=True)
class Jump:
    """A VTIMEZONE part: from which (local) moment the offset applies; the rule repeats it every year on such a day."""

    offset: datetime.timedelta
    start: datetime.datetime
    rule: dict[str, str]


def _field(line: str) -> tuple[str, dict[str, str], str]:
    """Parses "DTSTART;TZID="(UTC+03:00) Moscow":20261023T100000" -> ("DTSTART", {"TZID": ...}, "20261023T100000"):
    a colon and a semicolon inside quotes are part of the parameter value."""
    quotes, boundary = False, len(line)
    for number, sign in enumerate(line):
        if sign == '"':
            quotes = not quotes
        elif sign == ":" and not quotes:
            boundary = number
            break
    name, *params = re.findall(r'(?:[^;"]|"[^"]*")+', line[:boundary]) or [""]
    return (
        name.upper(),
        {k.upper(): z.strip('"') for k, _, z in (p.partition("=") for p in params)},
        line[boundary + 1 :],
    )


def _text(value: str) -> str:
    return value.replace("\\n", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def _offset(text: str) -> datetime.timedelta | None:
    found = re.fullmatch(r"([+-])(\d\d)(\d\d)", text.strip())
    if not found:
        return None
    return (-1 if found[1] == "-" else 1) * datetime.timedelta(hours=int(found[2]), minutes=int(found[3]))


def _jump(part: dict[str, str]) -> Jump | None:
    """A STANDARD or DAYLIGHT part; a broken one is skipped, the other parts of the zone stay."""
    if (shift := _offset(part.get("TZOFFSETTO", ""))) is None or "DTSTART" not in part:
        return None
    try:
        start = datetime.datetime.strptime(part["DTSTART"][:15], "%Y%m%dT%H%M%S")
    except ValueError:
        log.warning("skipped a time zone part that cannot be parsed: %s", part["DTSTART"][:40])
        return None
    return Jump(shift, start, _rule(part.get("RRULE", "")))


def _zones(lines: list[str]) -> dict[str, list[Jump]]:
    """VTIMEZONE time zones with standard and daylight time: Outlook and Google put them into the calendar itself."""
    result: dict[str, list[Jump]] = {}
    zone, part = "", None
    for line in lines:
        name, _, value = _field(line)
        if name == "TZID" and part is None:
            zone = value
        elif name == "BEGIN" and value in ("STANDARD", "DAYLIGHT"):
            part = {}
        elif name == "END" and value in ("STANDARD", "DAYLIGHT") and part is not None:
            if jump := _jump(part):
                result.setdefault(zone, []).append(jump)
            part = None
        elif part is not None:
            part[name] = value
    return result


def _in_month(year: int, month: int, day: str) -> datetime.date | None:
    """For "2TU" the second Tuesday of the month, for "-1SU" the last Sunday; without a number, not handled here."""
    found = re.fullmatch(r"([+-]?\d)?(MO|TU|WE|TH|FR|SA|SU)", day)
    if not found or not found[1]:
        return None
    number, week_of_day = int(found[1]), DAYS[found[2]]
    first_value = datetime.date(year, month, 1)
    if number > 0:
        result = first_value + datetime.timedelta(days=(week_of_day - first_value.weekday()) % 7 + 7 * (number - 1))
    else:
        following = datetime.date(year + month // 12, month % 12 + 1, 1)
        last_value = following - datetime.timedelta(days=1)
        result = last_value - datetime.timedelta(days=(last_value.weekday() - week_of_day) % 7 + 7 * (-number - 1))
    return result if result.month == month else None


def _offset_in(jumps: list[Jump], moment: datetime.datetime) -> datetime.timedelta:
    """Time zone offset at this local moment: the last transition no later than it (this year or the previous one)."""
    best_one: tuple[datetime.datetime, datetime.timedelta] | None = None
    for p in jumps:
        for year in (moment.year - 1, moment.year):
            try:
                day = (
                    _in_month(year, int(p.rule.get("BYMONTH", "0")), p.rule.get("BYDAY", ""))
                    if p.rule.get("FREQ") == "YEARLY" and p.rule.get("BYMONTH")
                    else None
                )
            except ValueError:  # a month 13 in the rule: the part counts from its start
                day = None
            when = datetime.datetime.combine(day, p.start.time()) if day else p.start
            if when <= moment and (best_one is None or when > best_one[0]):
                best_one = (when, p.offset)
    return best_one[1] if best_one else jumps[0].offset


def _time(value: str, params: dict[str, str], zones: dict[str, list[Jump]]) -> datetime.datetime:
    """ICS time to the computer's local time: "...Z" is UTC, TZID uses the VTIMEZONE zone (with daylight time),
    a zone missing from the calendar leaves the time as is."""
    if "T" not in value:
        return datetime.datetime.strptime(value[:8], "%Y%m%d")
    moment = datetime.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return moment.replace(tzinfo=datetime.UTC).astimezone().replace(tzinfo=None)
    jumps = zones.get(params.get("TZID", ""))
    if not jumps:
        return moment
    return (moment - _offset_in(jumps, moment)).replace(tzinfo=datetime.UTC).astimezone().replace(tzinfo=None)


def _participant(params: dict[str, str], value: str) -> str:
    """Participant name, or the email without a name; rooms and resources are not participants."""
    if params.get("CUTYPE", "").upper() in ("ROOM", "RESOURCE"):
        return ""
    return params.get("CN") or value.removeprefix("mailto:").removeprefix("MAILTO:")


def _events(lines: list[str]) -> list[dict]:
    """Raw VEVENTs: fields as is, participants as a list. Nested parts (a VALARM reminder with its own SUMMARY and
    ATTENDEE) are not event fields."""
    result, current_value, nested_count = [], None, 0
    for line in lines:
        name, params, value = _field(line)
        if name == "BEGIN" and value == "VEVENT":
            current_value, nested_count = {"участники": [], "EXDATE": []}, 0
        elif name == "END" and value == "VEVENT" and current_value is not None:
            result.append(current_value)
            current_value = None
        elif current_value is not None and name in ("BEGIN", "END"):
            nested_count += 1 if name == "BEGIN" else -1
        elif current_value is not None and not nested_count:
            if name == "ATTENDEE":
                current_value["участники"].append(_participant(params, value))
            elif name == "EXDATE":
                current_value["EXDATE"] += [(z, params) for z in value.split(",")]
            else:
                current_value[name] = (value, params)
    return result


def _rule(text: str) -> dict[str, str]:
    return dict(part.partition("=")[::2] for part in text.split(";") if part)


def _step(start: datetime.datetime, rule: dict[str, str], number: int) -> list[datetime.datetime]:
    """Repeats for the step number: a day, a week (on BYDAY days) or a month."""
    interval = int(rule.get("INTERVAL", "1")) * number
    frequency = rule.get("FREQ")
    if frequency == "DAILY":
        return [start + datetime.timedelta(days=interval)]
    if frequency == "WEEKLY":
        week = start - datetime.timedelta(days=start.weekday()) + datetime.timedelta(weeks=interval)
        days = [DAYS[d[-2:]] for d in rule.get("BYDAY", "").split(",") if d[-2:] in DAYS] or [start.weekday()]
        return sorted(week + datetime.timedelta(days=d) for d in days)
    if frequency == "MONTHLY":
        month = start.month - 1 + interval
        year, month = start.year + month // 12, month % 12 + 1
        if rule.get("BYDAY"):  # "second Tuesday", "last Friday"
            days = (_in_month(year, month, d) for d in rule["BYDAY"].split(","))
            return sorted(datetime.datetime.combine(d, start.time()) for d in days if d)
        try:
            return [start.replace(year=year, month=month)]
        except ValueError:  # the 31st in a short month
            return []
    if frequency == "YEARLY":
        try:
            return [start.replace(year=start.year + interval)]
        except ValueError:  # February 29
            return []
    return []


def repeats(
    start: datetime.datetime, rule_: str, until: datetime.datetime, exceptions: set[datetime.datetime]
) -> list[datetime.datetime]:
    """Starts of all repeats no later than until: with an UNTIL end or a COUNT number, without exceptions. A rule
    the program does not fully understand (BYSETPOS, BYWEEKNO and the like) is not expanded: only the first meeting,
    not made-up dates."""
    rule = _rule(rule_)
    if rule.keys() - RULE_PLAIN or (rule.get("FREQ") == "YEARLY" and rule.keys() & {"BYDAY", "BYMONTH"}):
        return [start] if start <= until and start not in exceptions else []
    end = (
        datetime.datetime.strptime(rule["UNTIL"][:8], "%Y%m%d") + datetime.timedelta(days=1)
        if "UNTIL" in rule
        else until
    )
    remaining = int(rule.get("COUNT", "0")) or REPEAT_LIMIT
    result = []
    for number in range(REPEAT_LIMIT):
        try:
            moments = _step(start, rule, number)
        except OverflowError:  # a huge interval: the next repeat is past the year 9999
            return result
        for moment in moments:
            if moment < start:
                continue
            if moment > min(end, until) or remaining <= 0:
                return result
            remaining -= 1
            if moment not in exceptions:
                result.append(moment)
    return result


def parse(text: str, until: datetime.datetime) -> list[Event]:
    """Calendar events with repeats up to the until moment; all-day meetings are skipped."""
    lines = _lines(text)
    zones = _zones(lines)
    raw_items = _events(lines)
    moved_items = set()
    for s in (s for s in raw_items if "RECURRENCE-ID" in s):
        with contextlib.suppress(ValueError, OverflowError):
            moved_items.add((s.get("UID", ("",))[0], _time(*s["RECURRENCE-ID"], zones)))
    result = []
    for s in raw_items:
        try:
            result += _event_repeats(s, zones, moved_items, until)
        except (ValueError, KeyError, IndexError, OverflowError):  # one corrupted event leaves the calendar working
            log.warning("skipped a calendar event that cannot be parsed", exc_info=True)
    return result


def _event_repeats(
    s: dict,
    zones: dict[str, list[Jump]],
    moved_items: set,
    until: datetime.datetime,
) -> list[Event]:
    if "DTSTART" not in s or "T" not in s["DTSTART"][0]:
        return []
    start = _time(*s["DTSTART"], zones)
    end = _time(*s["DTEND"], zones) if "DTEND" in s else start + datetime.timedelta(hours=1)
    exceptions = {_time(z, p, zones) for z, p in s["EXDATE"]}
    exceptions |= {m for uid, m in moved_items if uid == s.get("UID", ("",))[0] and "RRULE" in s}
    starts = repeats(start, s["RRULE"][0], until, exceptions) if "RRULE" in s else [start]
    shared = {
        "title": _text(s.get("SUMMARY", ("",))[0]),
        "participants": tuple(u for u in s["участники"] if u),
        "organizer": _participant(s["ORGANIZER"][1], s["ORGANIZER"][0]) if "ORGANIZER" in s else "",
    }
    return [Event(start=m, end=m + (end - start), **shared) for m in starts]


def current_value(events: list[Event], moment: datetime.datetime) -> Event | None:
    """The meeting going on or starting within 15 minutes; of several, the one that started closest to the moment."""
    running_all = [s for s in events if s.start - AHEAD <= moment < s.end]
    return min(running_all, key=lambda s: abs(s.start - moment)) if running_all else None


def meeting_now(moment: datetime.datetime | None = None) -> Event | None:
    """The current meeting by the calendar link from the Windows credential store; None without a link."""
    url = settings.key(KEY)
    if not url:
        return None
    moment = moment or datetime.datetime.now()
    return current_value(parse(providers.fetch_secret(url, tr("calendar")), moment + AHEAD), moment)


def upcoming(url: str, moment: datetime.datetime | None = None, days_count: int = 7) -> list[Event]:
    """Meetings by the calendar link for the coming days, by start time: checks the link in settings."""
    moment, text = moment or datetime.datetime.now(), providers.fetch_secret(url, tr("calendar"))
    if "BEGIN:VCALENDAR" not in text.upper():  # a sign-in page: the link is not a public calendar
        raise providers.ProviderError(tr("The link opened, but it is not an ICS calendar"))
    events = parse(text, moment + datetime.timedelta(days=days_count))
    return sorted((s for s in events if s.end > moment), key=lambda s: s.start)


def apply(folder: pathlib.Path, event: Event, recordings: pathlib.Path) -> None:
    """Meeting title and participants come from the calendar; the project is that of the previous meeting with the same
    title (a series)."""
    participants = list(dict.fromkeys(u for u in (event.organizer, *event.participants) if u))  # without duplicates
    fields: dict[str, object] = {"название": event.title, "участники_календаря": participants}
    if not catalog.properties(folder).get("проект"):
        previous_meeting = next(
            (v for v in catalog.list_(recordings) if v.folder != folder and v.project and v.heading == event.title),
            None,
        )
        if previous_meeting:
            fields["проект"] = previous_meeting.project
    catalog.set_(folder, **fields)
