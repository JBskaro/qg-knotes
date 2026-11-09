# SPDX-License-Identifier: GPL-3.0-only
"""Meeting catalog: folder status, info, custom title and project, groups by day, interrupted recordings."""

import datetime
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from knotes import recording, catalog, transcript, language


class TestCatalog(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.recordings = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _folder(self, name: str, *files: str) -> pathlib.Path:
        folder = self.recordings / name
        folder.mkdir()
        for file in files:
            (folder / file).write_text("x", encoding="utf-8")
        return folder

    def test_states(self):
        self._folder("пустая")
        self._folder("записана", "микрофон.flac")
        self._folder("прервана", "запись.json", "микрофон_01.wav")
        transcript.save(
            self._folder("готова", "общая.flac"),
            [],
            {"длительность": 38 * 60 + 28, "провайдер": "Scribe", "стоимость": 0.141},
            [],
        )
        catalog.mark_error(self._folder("ошибка", "микрофон.flac"), "нет ключа")
        result = {v.title: v for v in catalog.list_(self.recordings)}
        self.assertEqual(
            {i: v.status for i, v in result.items()},
            {"записана": "записана", "прервана": "прервана", "готова": "готова", "ошибка": "ошибка"},
        )
        self.assertEqual((result["готова"].seconds, result["готова"].cost), (38 * 60 + 28, 0.141))
        self.assertEqual(result["ошибка"].error, "нет ключа")
        catalog.clear_error(result["ошибка"].folder)
        self.assertEqual(catalog.meeting(result["ошибка"].folder).status, "записана")

    def test_ready_file_in_recordings_folder(self):
        (self.recordings / "Созвон.mp4").write_bytes(b"x")
        (self.recordings / "Взят.mp4").write_bytes(b"x")
        self._folder("Взят", "общая.flac")
        (self.recordings / "заметки.txt").write_text("x", encoding="utf-8")
        result = {v.title: v.status for v in catalog.list_(self.recordings)}
        self.assertEqual(result, {"Созвон.mp4": catalog.FILE, "Взят": catalog.RECORDED})

    def _ready_meeting(self, name: str, text: str, cost: float = 0.0, seconds: float = 0.0) -> pathlib.Path:
        folder = self._folder(name, "микрофон.flac")
        (folder / "расшифровка.md").write_text(f"# Встреча {name}\n\n{text}\n", encoding="utf-8")
        if seconds:
            answers = {"микрофон": [{"сдвиг": 0, "ответ": {"usage": {"seconds": seconds, "cost": cost}}}]}
            (folder / "scribe.json").write_text(json.dumps(answers), encoding="utf-8")
        return folder

    def test_search_in_transcripts(self):
        first = self._ready_meeting("а", "Обсудили ЁЛКУ и елку")
        self._ready_meeting("б", "Про бюджет")
        meetings = catalog.list_(self.recordings)
        self.assertEqual(catalog.find(meetings, "елку"), {first: 2})
        self.assertEqual(catalog.find(meetings, "нет такого"), {})

    def test_price_per_second_from_past(self):
        self.assertIsNone(catalog.price_per_second(catalog.list_(self.recordings)))
        self._ready_meeting("а", "т", 0.2, 3600)
        self._ready_meeting("б", "т", 0.04, 400)
        self.assertAlmostEqual(catalog.price_per_second(catalog.list_(self.recordings)), 0.24 / 4000)

    def test_list_rereads_only_changed_meetings(self):
        """The window refreshes the list every few seconds: an unchanged meeting is not re-read from disk."""
        folder = self._ready_meeting("2026-10-24_10-00", "т")
        catalog.list_(self.recordings)
        with mock.patch.object(catalog.transcript, "phrases", wraps=catalog.transcript.phrases) as phrases:
            catalog.list_(self.recordings)
            self.assertEqual(phrases.call_count, 0)
            catalog.set_(folder, title="Планерка")
            self.assertEqual(catalog.list_(self.recordings)[0].custom_title, "Планерка")
            self.assertEqual(phrases.call_count, 1)
        catalog.list_(self.recordings)[0].custom_title = "испорчено"  # the list returns copies
        self.assertEqual(catalog.list_(self.recordings)[0].custom_title, "Планерка")

    def test_old_audio_cleanup(self):
        old_one = self._ready_meeting("старая", "т")
        fresh = self._ready_meeting("свежая", "т")
        (old_one / "видео.mp4").write_bytes(b"mp4")
        self._folder("не_распознана", "микрофон.flac")
        os.utime(old_one / "расшифровка.md", (time.time() - 40 * 86400,) * 2)
        cleaned = catalog.clear_audio(catalog.list_(self.recordings), 30)
        self.assertEqual(cleaned, [old_one])
        self.assertFalse((old_one / "микрофон.flac").exists())
        self.assertFalse((old_one / "видео.mp4").exists())  # the video is deleted together with the audio
        self.assertTrue((fresh / "микрофон.flac").exists())
        self.assertTrue((self.recordings / "не_распознана" / "микрофон.flac").exists())

    def test_info_from_phrases_file(self):
        folder = self._folder("2026-10-22_10-00", "микрофон.flac")
        (folder / "расшифровка.md").write_text("# x\n", encoding="utf-8")
        phrases = {
            "сведения": {"длительность": 2280.0, "стоимость": 0.12, "провайдер": "x"},
            "заметки": [],
            "фразы": [{"кто": k, "начало": 0, "конец": 1, "текст": "т"} for k in ("Я", "Собеседник 1", "Я")],
        }
        (folder / "фразы.json").write_text(json.dumps(phrases, ensure_ascii=False), encoding="utf-8")
        v = catalog.meeting(folder)
        self.assertEqual((v.seconds, v.cost, v.participant_count), (2280.0, 0.12, 2))
        self.assertEqual(catalog.summary(v), "10:00 · 38 мин · 2 участника")  # prices are in the summary, per meeting

    def test_own_title_and_project(self):
        folder = self._folder("2026-10-22_10-18", "микрофон.flac")
        v = catalog.meeting(folder)
        self.assertEqual(v.heading, f"Встреча 22.10{'' if datetime.date.today().year == 2026 else '.2026'} 10:18")
        catalog.set_(folder, title="Отчет по продажам", project="Склад", seconds=125.0)
        v = catalog.meeting(folder)
        self.assertEqual(
            (v.heading, v.project, catalog.heading(folder)), ("Отчет по продажам", "Склад", "Отчет по продажам")
        )
        self.assertEqual(catalog.summary(v), "10:18 · 2 мин · запись")
        self.assertEqual(catalog.find([v], "продаж"), {folder: 1})
        self.assertEqual(catalog.projects([v, catalog.Meeting(folder, "x", project="Абв"), v]), ["Абв", "Склад"])
        catalog.set_(folder, title="")
        self.assertEqual(catalog.properties(folder), {"проект": "Склад", "секунд": 125.0})

    def test_price_breakdown(self):
        self.assertEqual(catalog.breakdown(0.46, 0.18), "речь 0,28 $, протоколы 0,18 $")
        self.assertEqual(catalog.breakdown(0.14, 0), "")  # no minutes, nothing to break down

    def test_broken_meeting_does_not_hide_others(self):
        """A list instead of a dict in PROPERTIES or a string in the seconds field: skip this meeting instead of
        breaking the list."""
        good = self._folder("2026-10-22_10-00", "микрофон.flac")
        bad = self._folder("2026-10-22_11-00", "микрофон.flac")
        (bad / catalog.PROPERTIES).write_text('{"секунд": "много"}', encoding="utf-8")
        another = self._folder("2026-10-22_12-00", "микрофон.flac")
        (another / catalog.PROPERTIES).write_text('["не словарь"]', encoding="utf-8")
        with self.assertLogs("knotes.catalog", "WARNING"):
            folders = [v.folder for v in catalog.list_(self.recordings)]
        self.assertIn(good, folders)
        self.assertIn(another, folders)  # not a dict: treated as no properties
        self.assertNotIn(bad, folders)

    def test_plural_by_number(self):
        forms = ("задача", "задачи", "задач")
        for count, word in [
            (1, "1 задача"),
            (2, "2 задачи"),
            (5, "5 задач"),
            (11, "11 задач"),
            (21, "21 задача"),
            (22, "22 задачи"),
            (112, "112 задач"),
        ]:
            with self.subTest(count=count):
                self.assertEqual(language.count(count, forms), word)

    def test_meeting_date_from_ready_file_by_file_date(self):
        """A meeting video from 19.10 taken into work on 21.10: the meeting is dated 19.10, the video lies beside it
        or in another folder."""
        recorded = datetime.datetime(2026, 10, 19, 23, 15).timestamp()
        beside = self.recordings / "Новая запись.mp4"
        beside.write_bytes(b"x")
        os.utime(beside, (recorded, recorded))
        self.assertEqual(
            catalog.folder_date(self._folder("Новая запись", "общая.flac")).date(), datetime.date(2026, 10, 19)
        )
        somewhere = self.recordings / "загрузки"
        somewhere.mkdir()
        video = somewhere / "Демо.mp4"
        video.write_bytes(b"x")
        os.utime(video, (recorded, recorded))
        folder = self._folder("Демо")
        (folder / "источник.txt").write_text(str(video), encoding="utf-8")
        self.assertEqual(catalog.folder_date(folder).date(), datetime.date(2026, 10, 19))
        self.assertEqual(catalog.folder_date(self._folder("Без видео")).date(), datetime.date.today())

    def test_title_of_last_year_and_file(self):
        self.assertEqual(
            catalog.default_title(pathlib.Path("2025-01-05_09-30"), datetime.date(2026, 1, 1)),
            "Встреча 05.01.2025 09:30",
        )
        self.assertEqual(catalog.default_title(pathlib.Path("Созвон.mp4")), "Созвон.mp4")
        self.assertEqual(catalog.summary(catalog.Meeting(pathlib.Path("а.mp4"), catalog.FILE)), "видео")
        self.assertEqual(catalog.summary(catalog.Meeting(pathlib.Path("а.m4a"), catalog.FILE)), "звук")

    def test_projects_count_move_rename(self):
        a, b = self._folder("2026-10-22_10-00", "микрофон.flac"), self._folder("2026-10-21_10-00", "микрофон.flac")
        (self.recordings / "Созвон.mp4").write_bytes(b"x")
        catalog.to_project([a, b, self.recordings / "Созвон.mp4"], "ЗУП")
        meetings = catalog.list_(self.recordings)
        self.assertEqual(catalog.account_by_projects(meetings), {"ЗУП": 2, "": 1})
        self.assertEqual(catalog.projects(meetings, ["Бухгалтерия", "ЗУП"]), ["Бухгалтерия", "ЗУП"])
        self.assertEqual(catalog.rename_project(meetings, "ЗУП", "Зарплата"), 2)
        self.assertEqual(catalog.meeting(a).project, "Зарплата")
        catalog.rename_project(catalog.list_(self.recordings), "Зарплата", "")
        self.assertEqual(catalog.account_by_projects(catalog.list_(self.recordings)), {"": 3})

    def test_groups_by_days(self):
        today = datetime.date(2026, 10, 22)  # Thursday
        days = [catalog.day(today - datetime.timedelta(days=d), today) for d in (0, 1, 2, 3, 4)]
        self.assertEqual(days, ["Сегодня", "Вчера", "На этой неделе", "На этой неделе", "Раньше"])

    def test_captions(self):
        self.assertEqual(
            [language.count(ch, catalog.PARTICIPANTS) for ch in (1, 2, 5, 11, 21, 23)],
            ["1 участник", "2 участника", "5 участников", "11 участников", "21 участник", "23 участника"],
        )
        self.assertEqual((catalog.duration(20), catalog.duration(3900)), ("1 мин", "1 ч 05 мин"))
        self.assertEqual((catalog.money(0.004), catalog.money(0.125)), ("меньше 0,01 $", "0,12 $"))

    def test_no_recordings_folder(self):
        self.assertEqual(catalog.list_(self.recordings / "нет"), [])

    def test_interrupted_except_current(self):
        interrupted_ = self._folder("а", recording.MANIFEST)
        current_one = self._folder("б", recording.MANIFEST)
        self.assertEqual(recording.interrupted_all(self.recordings, except_=current_one), [interrupted_])

    def test_new_folder_does_not_overwrite_existing(self):
        first = recording.new_folder(self.recordings)
        second = recording.new_folder(self.recordings)
        self.assertNotEqual(first, second)
        self.assertTrue(second.name.startswith(first.name))


if __name__ == "__main__":
    unittest.main()
