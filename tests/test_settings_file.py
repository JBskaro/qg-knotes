# SPDX-License-Identifier: GPL-3.0-only
"""The settings file: a field of the wrong type falls back to its default, a corrupted file that cannot be moved
does not stop the start, the MCP server leaves a corrupted file where it is."""

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from knotes import settings


class TestSettingsFieldTypes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name)
        self.path = self.folder / "настройки.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_null_list_falls_back_to_default_and_the_rest_stays(self):
        self._write({"язык": "en", "приложения_встреч": None, "мое_имя": "Скаро"})
        with self.assertLogs(level="WARNING") as log:
            config, problem = settings.read(self.path)
        self.assertEqual((config.language, config.my_name), ("en", "Скаро"))
        self.assertEqual(config.meeting_apps, list(settings.MEETING_APPS))
        self.assertIn("испорчен", problem)
        self.assertIn("meeting_apps", "\n".join(log.output))
        broken = json.loads((self.folder / "настройки.испорчены.json").read_text(encoding="utf-8"))
        self.assertIsNone(broken["приложения_встреч"])
        self.assertEqual(settings.read(self.path), (config, ""))  # the file is repaired: the message comes once

    def test_each_kind_of_wrong_type(self):
        self._write(
            {
                "предлагать_запись": "да",
                "удалять_звук_через": True,
                "проекты": ["Альфа", 3],
                "позиция_строки": "10,20",
                "раскладка": [],
                "вид_проектов": {"Альфа": "красный"},
                "словарь": [{"правильно": "Claude"}],
                "место_блокнота": None,
            }
        )
        with self.assertLogs(level="WARNING"):
            config = settings.read(self.path)[0]
        by_default = settings.Settings()
        self.assertEqual(
            (config.suggest_recording, config.delete_audio_after, config.projects, config.bar_position),
            (by_default.suggest_recording, by_default.delete_audio_after, [], None),
        )
        self.assertEqual((config.window_layout, config.project_looks), ({}, {}))
        self.assertEqual(config.vocabulary, [{"правильно": "Claude"}])  # right types stay

    def test_corrupted_file_that_cannot_be_moved(self):
        self.path.write_text("{испорчен", encoding="utf-8")
        with (
            mock.patch.object(pathlib.Path, "replace", side_effect=PermissionError(13, "in use")),
            self.assertLogs(level="WARNING"),
        ):
            config, problem = settings.read(self.path)
        self.assertEqual(config, settings.Settings())
        self.assertIn("испорчен", problem)
        self.assertTrue(self.path.exists())

    def test_mcp_server_leaves_corrupted_file_in_place(self):
        """settings.load() runs on every MCP tool call: it neither moves nor copies the file."""
        self.path.write_text("{испорчен", encoding="utf-8")
        self.assertEqual(settings.load(self.path), settings.Settings())
        self._write({"язык": "en", "приложения_встреч": None})
        self.assertEqual(settings.load(self.path).language, "en")
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["настройки.json"])
        self.assertIsNone(json.loads(self.path.read_text(encoding="utf-8"))["приложения_встреч"])


if __name__ == "__main__":
    unittest.main()
