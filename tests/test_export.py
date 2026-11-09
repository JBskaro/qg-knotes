# SPDX-License-Identifier: GPL-3.0-only
"""Export to PDF and DOCX: the minutes as in the email, the transcript with times and names, the files themselves."""

import io
import pathlib
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from unittest import mock

from PySide6.QtGui import QTextBlockFormat
from PySide6.QtWidgets import QApplication

from knotes import catalog, export, notes, transcript

app = QApplication.instance() or QApplication([])
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MINUTES = (
    "# Итоги встречи по складу\n\nКоллеги, добрый день! Итоги ниже.\n\nУчастники - Дмитрий, Игорь\n\n"
    "> черновик\n\n## Решения\n\n1. Цена берется из **прайса**. [05:40]\n\n## Задачи\n\n"
    "1. Доделать загрузку остатков. Игорь, до пятницы. [09:10] ✎\n2. Change type <String> to <Number>. Дмитрий.\n\n"
    "## Для аналитика\n\n1. К задаче 1. Задвоение. [11:10]\n\nРасшифровка - 2026-10-14_15-00, расшифровка.md\n"
)


def paragraphs(data: bytes) -> list[tuple[str, list[tuple[str, bool]]]]:
    """(style, [(text, bold)]) of every paragraph of document.xml."""
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        body = ET.fromstring(package.read("word/document.xml")).find(f"{W}body")  # noqa: S314 - our own file
    result = []
    for paragraph in body.iter(f"{W}p"):
        style = paragraph.find(f"{W}pPr/{W}pStyle")
        runs = [
            ("".join(t.text or "" for t in r.iter(f"{W}t")), r.find(f"{W}rPr/{W}b") is not None)
            for r in paragraph.iter(f"{W}r")
        ]
        result.append((style.get(f"{W}val") if style is not None else "", [r for r in runs if r[0]]))
    return result


class TestMinutesDocument(unittest.TestCase):
    def setUp(self):
        self.document = export.minutes_document(MINUTES, "Встреча 14.10")

    def test_text_of_the_email(self):
        """The same text as the email: no time marks, no note sign, no analyst section, no remarks."""
        text = self.document.text()
        self.assertEqual(self.document.title, "Итоги встречи по складу")
        for there in ("Коллеги, добрый день!", "Дмитрий, Игорь", "Цена берется из прайса.", "<String>"):
            self.assertIn(there, text)
        for gone in ("05:40", "✎", "Для аналитика", "Задвоение", "черновик", "расшифровка.md", "**"):
            self.assertNotIn(gone, text)

    def test_structure_headings_items_and_who_in_bold(self):
        kinds = [(b.kind, b.number) for b in self.document.blocks]
        self.assertEqual(
            kinds,
            [
                (export.TITLE, ""),
                (export.TEXT, ""),
                (export.TEXT, ""),
                (export.HEADING, ""),
                (export.ITEM, "1."),
                (export.HEADING, ""),
                (export.ITEM, "1."),
                (export.ITEM, "2."),
            ],
        )
        task = self.document.blocks[6]
        self.assertEqual([(r.text, r.bold) for r in task.runs][-1], ("Игорь, до пятницы.", True))
        self.assertIn(("прайса", True), [(r.text, r.bold) for r in self.document.blocks[4].runs])
        self.assertTrue(self.document.blocks[2].runs[0].quiet)  # the "Участники - " label

    def test_minutes_without_heading_get_the_meeting_title(self):
        document = export.minutes_document("## Решения\n\n1. Да.\n", "Встреча 14.10")
        self.assertEqual((document.blocks[0].kind, document.title), (export.TITLE, "Встреча 14.10"))


class TestFiles(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = pathlib.Path(self._temporary.name)
        self.folder = self.root / "2026-10-14_15-00"
        self.folder.mkdir()
        transcript.save(
            self.folder,
            [
                {"начало": 2.0, "конец": 4.0, "кто": "Я", "текст": "Смотрим загрузку."},
                {"начало": 5.0, "конец": 7.0, "кто": "Я", "текст": "И цены."},
                {"начало": 65.0, "конец": 70.0, "кто": "Собеседник 1", "текст": "Нарядили ёлку & <b>шары</b>."},
            ],
            {"длительность": 38 * 60, "провайдер": "OpenRouter", "стоимость": 0.1},
            [],
        )
        transcript.set_names(self.folder, {"Собеседник 1": "Анна"})
        transcript.mark(self.folder, 3.0)
        notes.add(self.folder, 4.0, "Заметка аналитика")

    def test_transcript_document(self):
        """Title, date, duration, participants, then each speaker with the times of the phrases; marks and the
        analyst's notes are not in it."""
        document = export.transcript_document(self.folder)
        text = document.text()
        for there in ("Встреча 14.10", "14.10.2026, 15:00", "38 мин", "Я, Анна", "00:02 Смотрим загрузку.", "01:05"):
            self.assertIn(there, text)
        self.assertNotIn("Отметка", text)
        self.assertNotIn("Заметка аналитика", text)
        speakers = [b.runs[0].text for b in document.blocks if b.kind == export.SPEAKER]
        self.assertEqual(speakers, ["Я", "Анна"])

    def test_not_transcribed(self):
        with self.assertRaises(FileNotFoundError):
            export.transcript_document(self.root)

    def test_docx_is_a_word_package(self):
        """The zip has the parts Word needs; the XML is well formed even with &, < and > in the text."""
        data = export.docx(export.transcript_document(self.folder))
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            names = package.namelist()
            for part in names:
                if part.endswith((".xml", ".rels")):
                    ET.fromstring(package.read(part))  # noqa: S314 - our own file
            styles = package.read("word/styles.xml").decode("utf-8")
        self.assertEqual(names[0], "[Content_Types].xml")
        self.assertIn("word/footer1.xml", names)
        self.assertIn('w:ascii="Segoe UI"', styles)
        texts = ["".join(t for t, _ in runs) for _, runs in paragraphs(data)]
        self.assertIn("Нарядили ёлку & <b>шары</b>.", texts[-1])
        self.assertEqual(paragraphs(data)[0][0], "Title")

    def test_docx_minutes_headings_and_bold(self):
        found = paragraphs(export.docx(export.minutes_document(MINUTES, "")))
        self.assertEqual([s for s, _ in found].count("Heading1"), 2)
        task = next(runs for _, runs in found if runs and runs[0][0] == "1." and "загрузку" in runs[1][0])
        self.assertEqual(task[-1], ("Игорь, до пятницы.", True))

    def test_pdf_layout_and_file(self):
        """The PDF: the text with Cyrillic as laid out by Qt, item text after its number lined up with the lines
        below it, a page number on each page."""
        document = export.minutes_document(MINUTES, "")
        long_item = export.Block(export.ITEM, (export.Run("Длинный пункт протокола " * 12),), "12.")
        document = export.Document(document.title, (*document.blocks, long_item))
        path = self.root / "протокол.pdf"
        layout = export.pdf_layout(document, export.pdf_writer(path, document))
        self.assertIn("Цена берется из прайса.", layout.toPlainText())
        block = layout.lastBlock()
        lines = block.layout()
        self.assertGreater(lines.lineCount(), 1)
        after_tab = lines.lineAt(0).cursorToX(len("12.\t"))[0]
        self.assertAlmostEqual(after_tab, lines.lineAt(1).x(), delta=2)
        dots = 300 / 72  # the space after a paragraph is the template's, in points of the page
        first, second = (
            layout.documentLayout().blockBoundingRect(b) for b in (block.previous().previous(), block.previous())
        )
        gap = second.top() - first.bottom()
        self.assertGreaterEqual(gap / dots, export.TEMPLATE.space_after)  # plus the line spacing below the line
        self.assertLess(gap / dots, export.TEMPLATE.space_after + 3)
        export.save(document, path)
        data = path.read_bytes()
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertIn(b"/Title", data)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["2026-10-14_15-00", "протокол.pdf"])

    def test_speaker_not_left_alone_at_the_bottom_of_a_page(self):
        """Whatever the length of the turns, a speaker's name is on the page of their first phrase."""
        writer = export.pdf_writer(self.root / "x.pdf", export.Document("", ()))
        moved = 0
        for count in range(20, 90, 2):  # wide enough to reach the bottom of a page with any font
            phrases = [export.Block(export.PHRASE, (export.Run(f"Фраза {n}."),), "00:01") for n in range(count)]
            turn = (export.Block(export.SPEAKER, (export.Run("Анна", True),)), *phrases)
            layout = export.pdf_layout(export.Document("", turn * 3), writer)
            pages, size = layout.pageCount(), layout.size()
            layout.markContentsDirty(0, layout.characterCount())  # laid out anew from scratch: nothing went missing
            self.assertEqual((layout.pageCount(), layout.size()), (pages, size), count)
            height, block = layout.pageSize().height(), layout.begin()
            while block.isValid():
                if block.text() == "Анна":
                    following = layout.documentLayout().blockBoundingRect(block.next()).top()
                    here = layout.documentLayout().blockBoundingRect(block).top()
                    self.assertEqual(int(following // height), int(here // height), count)
                    always = QTextBlockFormat.PageBreakFlag.PageBreak_AlwaysBefore
                    moved += bool(block.blockFormat().pageBreakPolicy() & always)
                block = block.next()
        self.assertGreater(moved, 0)  # some of them did fall at the bottom of a page

    def test_long_transcript_has_pages(self):
        phrases = [{"начало": n * 5.0, "конец": n * 5 + 4.0, "кто": "Я", "текст": f"Фраза {n}."} for n in range(400)]
        transcript.save(self.folder, phrases, {"длительность": 2000, "провайдер": "x", "стоимость": 0.0}, [])
        path = self.root / "расшифровка.pdf"
        export.save(export.transcript_document(self.folder), path)
        self.assertGreater(path.read_bytes().count(b"/Type /Page\n") + path.read_bytes().count(b"/Type /Page "), 3)

    def test_file_name_from_meeting(self):
        self.assertEqual(export.file_name("Протокол", self.folder), "Протокол - Встреча 14.10 15-00 - 14.10.2026")
        catalog.set_(self.folder, title='Склад: "итоги" 14.10.2026?')
        self.assertEqual(export.file_name("Протокол", self.folder), "Протокол - Склад- -итоги- 14.10.2026")
        self.assertEqual(export.with_extension(pathlib.Path("итог"), export.DOCX).name, "итог.docx")
        self.assertEqual(export.with_extension(pathlib.Path("итог.PDF"), export.DOCX).name, "итог.PDF")

    def test_file_in_use_keeps_the_old_one(self):
        """Word holds the old file: the new one is not half written over it, the temporary one is gone."""
        path = self.root / "протокол.docx"
        path.write_bytes(b"old")
        held = PermissionError(13, "Access is denied", str(path))
        with mock.patch.object(export.os, "replace", side_effect=held), self.assertRaises(PermissionError) as raised:
            export.save(export.minutes_document(MINUTES, ""), path)
        self.assertEqual(path.read_bytes(), b"old")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["2026-10-14_15-00", "протокол.docx"])
        self.assertIn("протокол.docx", export.failure_text(raised.exception, path))
        full = OSError(28, "No space left on device", str(path))
        self.assertIn("закончилось место", export.failure_text(full, path))
        with self.assertRaises(ValueError):
            export.save(export.minutes_document(MINUTES, ""), self.root / "протокол.txt")

    def test_pdf_that_cannot_be_written(self):
        with mock.patch.object(export.QPainter, "begin", return_value=False), self.assertRaises(OSError):
            export.save(export.minutes_document(MINUTES, ""), self.root / "протокол.pdf")
        self.assertEqual([p.name for p in self.root.iterdir()], ["2026-10-14_15-00"])


if __name__ == "__main__":
    unittest.main()
