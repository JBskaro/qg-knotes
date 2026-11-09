# SPDX-License-Identifier: GPL-3.0-only
"""Calendar by an ICS link: the current meeting with title and participants, repeats, time zones, exceptions,
moved occurrences, a meeting series gets the same project."""

import datetime
import pathlib
import tempfile
import unittest
from unittest import mock

from knotes import ics_calendar, catalog, providers, transcript
from knotes import settings

ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VTIMEZONE
TZID:Russian Standard Time
BEGIN:STANDARD
DTSTART:16010101T000000
TZOFFSETFROM:+0300
TZOFFSETTO:+0300
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:planerka
SUMMARY:Планерка по бюджету
DTSTART;TZID=Russian Standard Time:20261005T100000
DTEND;TZID=Russian Standard Time:20261005T103000
RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TH;UNTIL=20261231T000000Z
EXDATE;TZID=Russian Standard Time:20261015T100000
ORGANIZER;CN=Анна Петрова:mailto:anna@example.com
ATTENDEE;CN=Анна Петрова;ROLE=REQ-PARTICIPANT:mailto:anna@example.com
ATTENDEE;CN="Игорь Сидоров":mailto:igor@example.com
ATTENDEE;CUTYPE=ROOM;CN=Переговорная 3:mailto:room3@example.com
ATTENDEE:mailto:petr@example.com
END:VEVENT
BEGIN:VEVENT
UID:planerka
RECURRENCE-ID;TZID=Russian Standard Time:20261022T100000
SUMMARY:Планерка по бюджету (перенос)
DTSTART;TZID=Russian Standard Time:20261022T150000
DTEND;TZID=Russian Standard Time:20261022T153000
END:VEVENT
BEGIN:VEVENT
UID:demo
SUMMARY:Показ доработки\\, загрузка остатков
DTSTART:20261023T070000Z
DTEND:20261023T080000Z
END:VEVENT
BEGIN:VEVENT
UID:holiday
SUMMARY:Выходной
DTSTART;VALUE=DATE:20261104
END:VEVENT
END:VCALENDAR
"""
MSK = datetime.timezone(datetime.timedelta(hours=3))


def _msk(*parts: int) -> datetime.datetime:
    """Moscow time to the local time of the computer running the test."""
    return datetime.datetime(*parts, tzinfo=MSK).astimezone().replace(tzinfo=None)


class TestCalendar(unittest.TestCase):
    def _now(self, *parts: int) -> ics_calendar.Event | None:
        moment = _msk(*parts)
        return ics_calendar.current_value(ics_calendar.parse(ICS, moment + ics_calendar.AHEAD), moment)

    def test_recurring_meeting_with_attendees(self):
        event = self._now(2026, 10, 19, 10, 5)
        self.assertEqual(event.title, "Планерка по бюджету")
        self.assertEqual(event.start, _msk(2026, 10, 19, 10, 0))
        self.assertEqual(event.participants, ("Анна Петрова", "Игорь Сидоров", "petr@example.com"))  # without the room
        self.assertEqual(event.organizer, "Анна Петрова")
        self.assertIsNotNone(self._now(2026, 10, 8, 9, 50))  # Thursday, 10 minutes before the start: already this one
        self.assertIsNone(self._now(2026, 10, 15, 10, 5))  # an excluded Thursday
        self.assertIsNone(self._now(2026, 10, 20, 10, 5))  # no Tuesday in the rule

    def test_moved_meeting_and_utc_time(self):
        self.assertIsNone(self._now(2026, 10, 22, 10, 5))  # not at 10:00 anymore
        self.assertEqual(self._now(2026, 10, 22, 15, 10).title, "Планерка по бюджету (перенос)")
        self.assertEqual(self._now(2026, 10, 23, 10, 20).title, "Показ доработки, загрузка остатков")
        self.assertIsNone(self._now(2026, 11, 4, 12, 0))  # an all-day event is not a meeting

    def test_repeat_count_and_every_other_day(self):
        start = datetime.datetime(2026, 10, 1, 9, 0)
        repeats = ics_calendar.repeats(start, "FREQ=DAILY;INTERVAL=2;COUNT=3", datetime.datetime(2027, 1, 1), set())
        self.assertEqual([m.day for m in repeats], [1, 3, 5])
        months = ics_calendar.repeats(start, "FREQ=MONTHLY", datetime.datetime(2026, 12, 15), set())
        self.assertEqual([m.month for m in months], [10, 11, 12])

    def test_reminder_dst_quotes_and_second_tuesday(self):
        """Google puts a reminder with SUMMARY inside the event; Outlook gives a time zone with daylight saving time
        and a quoted name; a monthly meeting on the "second Tuesday"."""
        ics = """BEGIN:VCALENDAR
BEGIN:VTIMEZONE
TZID:W. Europe Standard Time
BEGIN:STANDARD
DTSTART:16010101T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010101T020000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:berlin
SUMMARY:Созвон с Берлином
DTSTART;TZID=W. Europe Standard Time:20260715T100000
DTEND;TZID=W. Europe Standard Time:20260715T110000
BEGIN:VALARM
ACTION:EMAIL
SUMMARY:Alarm notification
ATTENDEE:mailto:me@example.com
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:quoted
SUMMARY:Сверка
DTSTART;TZID="(UTC+03:00) Moscow, St. Petersburg":20261023T120000
DTEND;TZID="(UTC+03:00) Moscow, St. Petersburg":20261023T130000
END:VEVENT
BEGIN:VEVENT
UID:monthly
SUMMARY:Статус проекта
DTSTART:20261013T070000Z
DTEND:20261013T080000Z
RRULE:FREQ=MONTHLY;BYDAY=2TU
END:VEVENT
BEGIN:VEVENT
UID:weird
SUMMARY:Непонятное правило
DTSTART:20261001T070000Z
DTEND:20261001T080000Z
RRULE:FREQ=MONTHLY;BYSETPOS=-1;BYDAY=MO,TU
END:VEVENT
END:VCALENDAR
"""
        until = datetime.datetime(2027, 1, 1)
        events = ics_calendar.parse(ics, until)
        berlin = next(s for s in events if s.title == "Созвон с Берлином")
        self.assertEqual(berlin.participants, ())  # the reminder email is not a participant
        self.assertEqual(berlin.start, _msk(2026, 7, 15, 11, 0))  # Berlin is +2 in summer, that is 11:00 Moscow time
        self.assertIn("Сверка", [s.title for s in events])  # quotes in the time zone do not break the calendar
        status_dates = sorted(s.start for s in events if s.title == "Статус проекта")
        self.assertEqual([m.day for m in status_dates], [13, 10, 8])  # 13.10, 10.11, 08.12: second Tuesdays
        self.assertEqual(len([s for s in events if s.title == "Непонятное правило"]), 1)  # do not make up dates

    def test_one_broken_event_or_zone_keeps_the_calendar(self):
        """A huge interval, a broken time zone part or a month 13 in its rule: the rest of the calendar works."""
        broken = """BEGIN:VCALENDAR
BEGIN:VTIMEZONE
TZID:Сломанная
BEGIN:STANDARD
DTSTART:2026ОКТЯБРЬ
TZOFFSETTO:+0500
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010101T020000
TZOFFSETTO:+0300
RRULE:FREQ=YEARLY;BYMONTH=13;BYDAY=-1SU
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:huge
SUMMARY:Огромный интервал
DTSTART:20261023T070000Z
DTEND:20261023T080000Z
RRULE:FREQ=DAILY;INTERVAL=999999999
END:VEVENT
BEGIN:VEVENT
UID:zone
SUMMARY:По сломанному поясу
DTSTART;TZID=Сломанная:20261023T120000
DTEND;TZID=Сломанная:20261023T130000
END:VEVENT
END:VCALENDAR
"""
        events = ics_calendar.parse(broken, _msk(2026, 10, 30))
        self.assertEqual(
            [(s.title, s.start) for s in events],
            [("Огромный интервал", _msk(2026, 10, 23, 10, 0)), ("По сломанному поясу", _msk(2026, 10, 23, 12, 0))],
        )

    def test_meeting_by_stored_link_and_series_to_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            recordings = pathlib.Path(tmp)
            previous_meeting = recordings / "2026-10-15_10-00"
            now = recordings / "2026-10-19_10-01"
            for folder in (previous_meeting, now):
                folder.mkdir()
                transcript.save(
                    folder,
                    [{"начало": 1.0, "конец": 2.0, "кто": "Я", "текст": "А."}],
                    {"длительность": 60, "провайдер": "x", "стоимость": 0.1},
                    [],
                )
            catalog.set_(previous_meeting, title="Планерка по бюджету", project="Склад")
            with (
                mock.patch.object(settings, "key", return_value="webcal://example.com/c.ics"),
                mock.patch.object(providers, "_curl", return_value=(200, ICS)) as request,
            ):
                event = ics_calendar.meeting_now(_msk(2026, 10, 19, 10, 2))
            self.assertEqual(request.call_args.args[2], "https://example.com/c.ics")
            self.assertTrue(request.call_args.kwargs["secret_address"])  # the secret link is not on the command line
            ics_calendar.apply(now, event, recordings)
            properties = catalog.properties(now)
            self.assertEqual((properties["название"], properties["проект"]), ("Планерка по бюджету", "Склад"))
            self.assertEqual(properties["участники_календаря"], ["Анна Петрова", "Игорь Сидоров", "petr@example.com"])
        with mock.patch.object(settings, "key", return_value=""):
            self.assertIsNone(ics_calendar.meeting_now())
        with self.assertRaises(providers.ProviderError):
            providers.fetch_secret("http://example.com/c.ics", "календарь")

    def test_upcoming_for_link_check(self):
        moment = _msk(2026, 10, 19, 10, 20)
        with mock.patch.object(providers, "fetch_secret", return_value=ICS):
            meetings = ics_calendar.upcoming("https://example.com/c.ics", moment)
        self.assertEqual(
            [(s.title, s.start) for s in meetings],
            [
                ("Планерка по бюджету", _msk(2026, 10, 19, 10, 0)),  # still going on
                ("Планерка по бюджету (перенос)", _msk(2026, 10, 22, 15, 0)),
                ("Показ доработки, загрузка остатков", _msk(2026, 10, 23, 10, 0)),
                ("Планерка по бюджету", _msk(2026, 10, 26, 10, 0)),
            ],
        )  # a week: until 26.10 10:20

    def test_quote_and_newline_do_not_extend_curl_config(self):
        """The address and the key go into the curl config as a quoted string: a quote or a line break would add a
        command of their own there, for example writing the response to a file."""
        for address in ('https://example.com/c.ics"\noutput = "C:/x.txt', "https://example.com/a b.ics"):
            with (
                self.subTest(address=address),
                mock.patch.object(providers.subprocess, "run") as startup,
                self.assertRaises(providers.ProviderError),
            ):
                providers.fetch_secret(address, "календарь")
            startup.assert_not_called()
        with mock.patch.object(providers.subprocess, "run") as startup, self.assertRaises(providers.ProviderError):
            providers._curl('ключ"\noutput = "x', [], "https://example.com")
        startup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
