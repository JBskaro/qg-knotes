# SPDX-License-Identifier: GPL-3.0-only
"""Transcript: phrases, participant names, marks, rebuilding расшифровка.md without recognizing again."""

import pathlib
import tempfile
import unittest

from knotes import transcript

PHRASES = [
    {"начало": 2.0, "конец": 4.0, "кто": "Я", "текст": "Добрый день.", "говорящий": "м/0"},
    {"начало": 5.0, "конец": 9.0, "кто": "Собеседник 1", "текст": "Здравствуйте.", "говорящий": "с/0"},
    {"начало": 70.0, "конец": 72.0, "кто": "Собеседник 2", "текст": "Согласен.", "говорящий": "с/1"},
]
INFO = {"длительность": 75.0, "стоимость": 0.004, "провайдер": "OpenRouter, elevenlabs/scribe-v2"}


class TestTranscript(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name) / "2026-10-22_10-00"
        self.folder.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _text(self) -> str:
        return (self.folder / transcript.TRANSCRIPT).read_text(encoding="utf-8")

    def test_save_and_assembly(self):
        transcript.save(self.folder, PHRASES, INFO, ["Заметка."])
        text = self._text()
        self.assertIn("Длительность 01:15. Распознано: OpenRouter, elevenlabs/scribe-v2, стоимость 0.004 $.", text)
        self.assertIn("Заметка.", text)
        self.assertIn("[00:05] Собеседник 1: Здравствуйте.", text)
        self.assertEqual(transcript.participants(self.folder), ["Я", "Собеседник 1", "Собеседник 2"])
        self.assertNotIn("говорящий", (self.folder / transcript.PHRASES).read_text(encoding="utf-8"))

    def test_names_instead_of_numbers(self):
        transcript.save(self.folder, PHRASES, INFO, [])
        transcript.set_names(self.folder, {"Собеседник 1": " Анна ", "Собеседник 2": "", "Я": "Я"})
        self.assertEqual(transcript.names(self.folder), {"Собеседник 1": "Анна"})
        text = self._text()
        self.assertIn("[00:05] Анна: Здравствуйте.", text)
        self.assertIn("[01:10] Собеседник 2: Согласен.", text)

    def test_marks_land_by_time(self):
        transcript.mark(self.folder, 6.04)
        transcript.mark(self.folder, 80, "★ Решение")
        transcript.save(self.folder, PHRASES, INFO, [])
        lines = [s for s in self._text().splitlines() if s.startswith("[")]
        self.assertEqual(lines[2], "[00:06] ★ Отметка")
        self.assertEqual(lines[-1], "[01:20] ★ Решение")
        self.assertEqual(transcript.labels(self.folder)[0]["время"], 6.0)

    def test_phrase_edit(self):
        transcript.save(self.folder, PHRASES, INFO, [])
        transcript.fix_phrase(self.folder, 1, " Здравствуйте, коллеги. ")
        self.assertIn("[00:05] Собеседник 1: Здравствуйте, коллеги.", self._text())
        self.assertEqual(transcript.phrases(self.folder)["фразы"][1]["текст"], "Здравствуйте, коллеги.")

    def test_speech_shares(self):
        transcript.save(self.folder, PHRASES, INFO, [])
        shares = transcript.shares(self.folder)
        self.assertAlmostEqual(shares["Собеседник 1"], 0.5)
        self.assertAlmostEqual(sum(shares.values()), 1.0)

    def test_consecutive_blocks_and_marks(self):
        phrases = [*PHRASES[:2], {"начало": 9.5, "конец": 10.0, "кто": "Собеседник 1", "текст": "Да."}, PHRASES[2]]
        transcript.save(self.folder, phrases, INFO, [])
        transcript.mark(self.folder, 9.5, "★ Решение")
        blocks = transcript.blocks(self.folder)
        self.assertEqual(
            [b.get("кто", b.get("метка")) for b in blocks],
            ["Я", "Собеседник 1", "★ Решение", "Собеседник 1", "Собеседник 2"],
        )
        self.assertEqual(blocks[1]["фразы"], [(1, 5.0, "Здравствуйте.")])
        self.assertEqual(blocks[3]["фразы"], [(2, 9.5, "Да.")])

    def test_unrecognized_cannot_be_composed(self):
        self.assertIsNone(transcript.phrases(self.folder))
        self.assertEqual(transcript.participants(self.folder), [])
        with self.assertRaises(FileNotFoundError):
            transcript.collect(self.folder)
        with self.assertRaises(FileNotFoundError):
            transcript.fix_phrase(self.folder, 0, "т")
        self.assertEqual((transcript.shares(self.folder), transcript.blocks(self.folder)), ({}, []))

    def test_time(self):
        self.assertEqual((transcript.time(5), transcript.time(3725)), ("00:05", "1:02:05"))


if __name__ == "__main__":
    unittest.main()
