# SPDX-License-Identifier: GPL-3.0-only
"""Files on disk: a write goes through a temporary file and never leaves half a file, a corrupted file is set aside
before the next write replaces it."""

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from knotes import catalog, settings, transcript


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_failed_write_keeps_old_file_and_leaves_no_temporary(self):
        path = self.folder / "задачи.json"
        transcript.write_json(path, {"было": 1})
        with (
            mock.patch.object(transcript.os, "replace", side_effect=OSError(28, "No space")),
            self.assertRaises(OSError),
        ):
            transcript.write_json(path, {"стало": 2})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"было": 1})
        self.assertEqual([p.name for p in self.folder.iterdir()], ["задачи.json"])

    def test_file_held_by_another_process_is_retried(self):
        path = self.folder / "встреча.json"
        real = transcript.os.replace
        calls = []

        def busy_twice(source: str, target: str) -> None:
            calls.append(target)
            if len(calls) < 3:
                raise PermissionError(13, "in use")
            real(source, target)

        with (
            mock.patch.object(transcript.os, "replace", side_effect=busy_twice),
            mock.patch.object(transcript.clock, "sleep"),
        ):
            transcript.write_json(path, {"название": "Созвон"})
        self.assertEqual(len(calls), 3)
        self.assertEqual(transcript.read_json(path, {}), {"название": "Созвон"})

    def test_held_for_too_long_fails_with_log(self):
        path = self.folder / "встреча.json"
        with (
            mock.patch.object(transcript.os, "replace", side_effect=PermissionError(13, "in use")),
            mock.patch.object(transcript.clock, "sleep"),
            self.assertLogs(level="WARNING"),
            self.assertRaises(PermissionError),
        ):
            transcript.write_json(path, {})
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_meeting_properties_and_settings_are_written_through_temporary_file(self):
        meeting = self.folder / "2026-10-22_10-00"
        meeting.mkdir()
        catalog.set_(meeting, title="Созвон")
        settings_file = self.folder / "настройки.json"
        settings.save(settings.Settings(language="en"), settings_file)
        with mock.patch.object(transcript.os, "replace", side_effect=OSError(5, "I/O")):
            with self.assertRaises(OSError):
                catalog.set_(meeting, title="Другое")
            with self.assertRaises(OSError):
                settings.save(settings.Settings(language="ru"), settings_file)
        self.assertEqual(catalog.properties(meeting), {"название": "Созвон"})
        self.assertEqual(settings.load(settings_file).language, "en")


class TestCorruptedFileSetAside(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_corrupted_file_is_copied_before_it_is_replaced(self):
        path = self.folder / "задачи.json"
        path.write_text('{"правки": {"к1": испорчен', encoding="utf-8")
        with self.assertLogs(level="WARNING") as log:
            self.assertEqual(transcript.read_json(path, {}), {})
        copy_ = self.folder / "задачи.испорчен.json"
        self.assertIn(copy_.name, "\n".join(log.output))
        transcript.write_json(path, {"правки": {}})
        self.assertEqual(copy_.read_text(encoding="utf-8"), '{"правки": {"к1": испорчен')

    def test_wrong_type_is_set_aside_too(self):
        path = self.folder / "имена.json"
        path.write_text('["Анна"]', encoding="utf-8")
        with self.assertLogs(level="WARNING"):
            self.assertEqual(transcript.read_json(path, {}), {})
        self.assertEqual((self.folder / "имена.испорчен.json").read_text(encoding="utf-8"), '["Анна"]')

    def test_copy_failure_does_not_stop_reading(self):
        path = self.folder / "метки.json"
        path.write_text("{", encoding="utf-8")
        with (
            mock.patch.object(pathlib.Path, "write_bytes", side_effect=PermissionError(13, "read-only")),
            self.assertLogs(level="WARNING"),
        ):
            self.assertEqual(transcript.read_json(path, []), [])


if __name__ == "__main__":
    unittest.main()
