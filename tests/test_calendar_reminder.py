# SPDX-License-Identifier: GPL-3.0-only
"""The calendar reminder: a meeting is reminded of at its start, once, not while recording, not when it started long
ago or lasts all day; the calendar is read again every quarter of an hour; the program asks «Meeting started, record?»
and records on «Record»."""

import dataclasses
import datetime
import unittest
from unittest import mock

from knotes import calendar_reminder, ics_calendar
from knotes.calendar_reminder import Reminder
from tests.test_app import program_for

NINE = datetime.datetime(2026, 10, 27, 9, 0)


def event(title: str, start: datetime.datetime, minutes: int = 60) -> ics_calendar.Event:
    return ics_calendar.Event(title, start, start + datetime.timedelta(minutes=minutes))


class TestReminder(unittest.TestCase):
    def setUp(self):
        self.reminder = Reminder()
        self.reminder.read([event("Загрузка остатков", NINE), event("Отпуск", NINE, 24 * 60)], NINE)

    def _due(self, minute: int, busy: bool = False) -> list[str]:
        return [s.title for s in self.reminder.due(NINE + datetime.timedelta(minutes=minute), busy)]

    def test_at_the_start_once(self):
        self.assertEqual(self._due(-1), [])  # not yet
        self.assertEqual(self._due(0), ["Загрузка остатков"])  # the all-day event is not a meeting
        self.assertEqual(self._due(1), [])  # once

    def test_not_late_and_not_while_recording(self):
        self.assertEqual(self._due(calendar_reminder.LATE_MINUTES), [])  # the computer slept through the start
        self.reminder = Reminder()
        self.reminder.read([event("Загрузка остатков", NINE)], NINE)
        self.assertEqual(self._due(2, busy=True), [])  # already recording
        self.assertEqual(self._due(3), [])  # and not after the recording either

    def test_calendar_read_again_every_quarter_of_an_hour(self):
        self.assertTrue(Reminder().stale(NINE))
        self.assertFalse(self.reminder.stale(NINE + datetime.timedelta(minutes=14)))
        self.assertTrue(self.reminder.stale(NINE + datetime.timedelta(minutes=15)))
        moved = event("Загрузка остатков", NINE + datetime.timedelta(minutes=30))  # moved: reminded at the new time
        self.reminder.read([moved], NINE + datetime.timedelta(minutes=15))
        self.assertEqual(self._due(30), ["Загрузка остатков"])


class TestCalendarQuestion(unittest.TestCase):
    def setUp(self):
        self.program = program_for(self)
        self.program.reminder.read([event("Загрузка остатков", NINE)], NINE)
        for patch in (
            mock.patch.object(calendar_reminder.settings, "key", return_value="https://calendar.example/ics"),
            mock.patch.object(self.program, "_now", return_value=NINE + datetime.timedelta(minutes=1)),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_meeting_started_record(self):
        with mock.patch.object(self.program.session, "start") as started:
            self.program._poll_calendar()
            heading, text, buttons, answer = self.program._notify_with_buttons.call_args.args
            self.assertEqual(heading, "Встреча началась")
            self.assertEqual(text, "«Загрузка остатков» по календарю. Записать?")
            self.assertEqual([b[1] for b in buttons], ["Записать", "Не сейчас"])
            answer("нет")
            started.assert_not_called()
            answer("записать")
            started.assert_called_once_with()  # the title and participants come from the calendar at the start
        self.program._poll_calendar()
        self.assertEqual(self.program._notify_with_buttons.call_count, 1)  # once

    def test_no_question_when_suggestions_are_off(self):
        self.program.settings = dataclasses.replace(self.program.settings, suggest_recording=False)
        self.program._poll_calendar()
        self.program._notify_with_buttons.assert_not_called()


if __name__ == "__main__":
    unittest.main()
