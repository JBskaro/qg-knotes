# SPDX-License-Identifier: GPL-3.0-only
"""Meeting notes: the notepad with recording minutes, the agenda, moving old notes, preparing the next meeting
from the analyst's questions, undiscussed items and the task board."""

import pathlib
import tempfile
import unittest

from knotes import notes, catalog, projects, transcript

TEXT = """## Повестка

- [x] Колонка «Остаток»
- [ ] Остановка расчета при ошибке

## Заметки

# Расчет
[02:01] пропуски строк без артикула
[1:02:03] Решение - цена из прайса в базе
после встречи: проверить по ТЗ
"""


class TestNotes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.recordings = pathlib.Path(self._tmp.name)
        self.folder = self._meeting("2026-10-19_15-00")

    def tearDown(self):
        self._tmp.cleanup()

    def _meeting(self, name: str, project: str = "Склад") -> pathlib.Path:
        folder = self.recordings / name
        folder.mkdir()
        transcript.save(
            folder,
            [{"начало": 5.0, "конец": 9.0, "кто": "Я", "текст": "Смотрим."}],
            {"длительность": 600, "провайдер": "x", "стоимость": 0.1},
            [],
        )
        catalog.set_(folder, project=project)
        return folder

    def test_parse_and_write_round_trip(self):
        parsed = notes.from_text(TEXT)
        self.assertEqual(
            parsed.agenda,
            (notes.AgendaItem("Колонка «Остаток»", True), notes.AgendaItem("Остановка расчета при ошибке")),
        )
        self.assertEqual(
            [(s.text, s.second) for s in parsed.lines],
            [
                ("# Расчет", None),
                ("пропуски строк без артикула", 121),
                ("Решение - цена из прайса в базе", 3723),
                ("после встречи: проверить по ТЗ", None),
            ],
        )
        self.assertEqual(notes.to_text(parsed), TEXT)
        no_agenda = notes.Notes(lines=(notes.NoteLine("одна", 5),))
        self.assertEqual(notes.to_text(no_agenda), "[00:05] одна\n")

    def test_own_lines_like_section_headings_keep_file(self):
        """The analyst typed "## Повестка" (agenda) or "## Заметки" (notes) as text: after reading the text is the
        same, the agenda does not eat those lines."""
        for notes_ in (
            notes.Notes(lines=(notes.NoteLine("## Повестка"), notes.NoteLine("- [ ] мое"), notes.NoteLine("просто"))),
            notes.Notes(
                (notes.AgendaItem("Сроки"),),
                (notes.NoteLine("## Заметки"), notes.NoteLine("## Повестка"), notes.NoteLine("итог")),
            ),
        ):
            with self.subTest(notes=notes_):
                self.assertEqual(notes.from_text(notes.to_text(notes_)), notes_)

    def test_note_from_transcript_goes_by_minute_keeping_rest(self):
        (self.folder / notes.FILE).write_text(
            "# Итоги\n[01:00] первая\nдописано после\n[05:00] вторая\n", encoding="utf-8"
        )
        notes.add(self.folder, 180, "новая")
        self.assertEqual(
            [s.text for s in notes.read(self.folder).lines], ["# Итоги", "первая", "дописано после", "новая", "вторая"]
        )

    def test_line_file_to_place_and_delete(self):
        self.assertTrue(notes.read(self.folder).empty)
        notes.add(self.folder, 300, "вторая")
        notes.add(self.folder, 61.7, " первая ")
        self.assertEqual(notes.with_minutes(self.folder), [(61, "первая"), (300, "вторая")])
        notes.delete(self.folder, 61, "первая")
        notes.delete(self.folder, 300, "вторая")
        self.assertFalse((self.folder / notes.FILE).exists())  # empty notes: no file

    def test_old_notes_from_marks_move_to_notepad(self):
        transcript.mark(self.folder, 7.0, f"{transcript.NOTE} цена из прайса")
        transcript.mark(self.folder, 8.0)
        self.assertEqual(notes.with_minutes(self.folder), [(7, "цена из прайса")])
        self.assertEqual(
            [m["текст"] for m in transcript.labels(self.folder)], [transcript.MARKER]
        )  # the star mark is still there
        self.assertNotIn(transcript.NOTE, (self.folder / transcript.TRANSCRIPT).read_text(encoding="utf-8"))

    def test_model_gets_agenda_and_notes(self):
        (self.folder / notes.FILE).write_text(TEXT, encoding="utf-8")
        input_ = "\n".join(notes.for_model(self.folder))
        for part in (
            "- [x] Колонка «Остаток»",
            "- [ ] Остановка расчета при ошибке",
            "[02:01] пропуски строк без артикула",
            "[1:02:03] Решение - цена из прайса",
            "после встречи: проверить по ТЗ",
        ):
            self.assertIn(part, input_)
        self.assertEqual(notes.for_model(self._meeting("2026-10-20_10-00")), [])

    def test_questions(self):
        for line, yes in (
            ("Вопрос - кто согласует", True),
            ("? сроки", True),
            ("- нужен ли итог?", True),
            ("Решение - цена из прайса", False),
            ("вопросов нет.", False),
        ):
            with self.subTest(line=line):
                self.assertEqual(notes.question(line), yes)

    def test_agenda_from_analyst_questions_last_meeting_and_board(self):
        """The analyst writes questions: the program takes them into the agenda of the next meeting by itself."""
        (self.folder / notes.FILE).write_text(TEXT, encoding="utf-8")
        projects.set_(self.recordings, "Склад", projects.ProjectNotes("ЗУП 3.1.\nКто подписывает акты?"))
        projects.set_for_meeting(self.recordings, "Склад", "- Сроки препрода\nОстановка расчета при ошибке.\n")
        new_one = self._meeting("2026-10-23_10-00")
        items = notes.agenda(
            self.recordings, "Склад", except_=new_one, from_board=("Нужна ли колонка «Остаток»?", "Сроки препрода")
        )
        self.assertEqual(
            [p.text for p in items],
            ["Сроки препрода", "Остановка расчета при ошибке.", "Кто подписывает акты?", "Нужна ли колонка «Остаток»?"],
        )
        self.assertEqual(notes.undiscussed(self.recordings, "Склад", except_=new_one), ["Остановка расчета при ошибке"])
        self.assertEqual(
            notes.agenda_text(items[:2], "Повестка"),
            "Повестка\n\n1. Сроки препрода\n2. Остановка расчета при ошибке.\n",
        )

    def test_discussed_leaves_meeting_questions(self):
        projects.set_for_meeting(self.recordings, "Склад", "- Сроки препрода\nКто подписывает акты")
        passed = notes.Notes((notes.AgendaItem("Сроки препрода", True), notes.AgendaItem("Кто подписывает акты")))
        notes.close_agenda(self.recordings, "Склад", passed)
        self.assertEqual(projects.for_meeting(self.recordings, "Склад"), "Кто подписывает акты")


if __name__ == "__main__":
    unittest.main()
