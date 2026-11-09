# SPDX-License-Identifier: GPL-3.0-only
"""Failures as the user sees them: texts, corrupted files, corrupted settings."""

import errno
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from knotes import catalog, failures, providers, transcript
from knotes import settings
from knotes import recognition


class TestTexts(unittest.TestCase):
    def test_failure_texts(self):
        for error, words in (
            (providers.NetworkError("Нет интернета"), "Нет интернета"),
            (subprocess.CalledProcessError(1, ["ffmpeg"]), "файл поврежден"),
            (OSError(errno.ENOSPC, "No space", "D:\\записи"), "закончилось место (D:\\записи)"),
            (PermissionError(errno.EACCES, "denied", "C:\\Windows"), "Нет доступа"),
            (FileNotFoundError(errno.ENOENT, "no", "E:\\записи"), "съемном"),
            (OSError(-9996, "Invalid output device"), "Звуковое устройство"),
            (OSError(errno.EIO, "I/O error"), "Ошибка диска"),
            (ValueError("странно"), "Непредвиденная ошибка: странно"),
        ):
            with self.subTest(error=error):
                self.assertIn(words, failures.text(error))


class TestBrokenFiles(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.recordings = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_broken_meeting_files_do_not_break_list(self):
        folder = self.recordings / "2026-10-22_10-00"
        folder.mkdir()
        (folder / "микрофон.flac").write_bytes(b"x")
        (folder / "расшифровка.md").write_text(
            "# x\n\nДлительность 01:00. Распознано: x, стоимость 0.010 $.\n", encoding="utf-8"
        )
        for file in ("фразы.json", "имена.json", "метки.json", "встреча.json", "scribe.json"):
            (folder / file).write_text("{испорчен", encoding="utf-8")
        with self.assertLogs(level="WARNING"):
            meetings = catalog.list_(self.recordings)
        self.assertEqual((meetings[0].status, meetings[0].seconds), (catalog.READY, 0))  # phrases cannot be read
        self.assertEqual(
            (transcript.names(folder), transcript.labels(folder), catalog.properties(folder)), ({}, [], {})
        )
        self.assertIsNone(catalog.price_per_second(meetings))
        self.assertEqual(transcript.read_json(folder / recognition.ANSWERS, {}), {})

    def test_broken_settings_are_set_aside(self):
        path = self.recordings / "настройки.json"
        path.write_text('{"язык": "en", испорчен', encoding="utf-8")
        config, problem = settings.read(path)
        self.assertEqual(config, settings.Settings())
        self.assertIn("испорчен", problem)
        self.assertTrue((self.recordings / "настройки.испорчены.json").exists())
        self.assertFalse(path.exists())
        path.write_text("[1, 2]", encoding="utf-8")
        self.assertIn("испорчен", settings.read(path)[1])
        settings.save(settings.Settings(language="en"), path)
        self.assertEqual(settings.read(path), (settings.Settings(language="en"), ""))

    def test_broken_video(self):
        file = self.recordings / "битое.mp4"
        file.write_bytes(b"not a video")
        with self.assertRaises(subprocess.CalledProcessError) as result:
            recognition.from_file(file, self.recordings)
        self.assertIn("файл поврежден", failures.text(result.exception))


class TestKeyOnNetwork(unittest.TestCase):
    def test_provider_answer_not_json(self):
        with (
            mock.patch.object(providers, "_curl", return_value=(200, "<html>")),
            self.assertRaises(providers.TemporaryError),
        ):
            providers.balance(providers.find("openrouter"), "ключ")


if __name__ == "__main__":
    unittest.main()
