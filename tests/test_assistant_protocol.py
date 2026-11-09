# SPDX-License-Identifier: GPL-3.0-only
"""Minutes written by the user's own AI assistant over MCP, next to any minutes preset: the task it gets is the same
as the paid service gets, saving keeps the previous text and marks who wrote it, the ready-made prompt for MCP
clients, the hint in the settings and the request copied from the meeting."""

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from knotes import ai_tools
from knotes import catalog, minutes, privacy, providers, transcript
from knotes import settings
from knotes.mcp import ToolError, reply
from knotes.ui import settings_window
from knotes.ui.minutes_view import MinutesView

program = QApplication.instance() or QApplication([])


class TestAssistant(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.recordings = pathlib.Path(self._tmp.name)
        self.config = settings.Settings(recordings_folder=str(self.recordings), my_name="Дмитрий")
        patcher = mock.patch.object(settings, "load", return_value=self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = self.recordings / "2026-10-20_10-00"
        self.folder.mkdir()
        transcript.save(
            self.folder,
            [{"начало": 5.0, "конец": 9.0, "кто": "Я", "текст": "Цену берем из прайса."}],
            {"длительность": 60, "провайдер": "тест", "стоимость": 0},
            [],
        )
        catalog.set_(self.folder, title="Загрузка остатков", project="Склад")

    def test_task_same_as_service_gets(self):
        job = ai_tools.TOOLS["get_protocol_task"].action(meeting=self.folder.name, kind="Показ доработки")
        params = minutes.params(self.config)
        self.assertEqual(job["instruction"], params.instruction)
        input_ = minutes.input_(self.folder, params, "Показ доработки", with_transcript=False)
        self.assertEqual(job["input"], f"{privacy.NOTE}\n\n{input_}")  # personal data goes to the assistant as labels
        self.assertNotIn("Цену берем из прайса.", job["input"])  # the transcript is read in pieces with get_transcript
        self.assertIn("get_transcript", job["transcript"])
        self.assertIn("save_protocol", job["how_to_save"])
        self.assertEqual(job["вид_встречи"], "Показ доработки")
        self.assertNotIn("вид", catalog.properties(self.folder))  # a reading tool saves nothing

    def test_save_unwraps_and_keeps_previous(self):
        (self.folder / minutes.FILE).write_text("# Прежний\n", encoding="utf-8")
        catalog.set_(self.folder, minutes_cost=0.2)
        result = ai_tools.TOOLS["save_protocol"].action(
            meeting=self.folder.name,
            text="```markdown\n# Итоги\n\n## Решения\n\n1. Цена из прайса. [00:05]\n```",
            model="Claude Opus 5.5",
        )
        self.assertEqual(
            (self.folder / minutes.FILE).read_text(encoding="utf-8"),
            "# Итоги\n\n## Решения\n\n1. Цена из прайса. [00:05]\n",
        )
        self.assertEqual((self.folder / ai_tools.PREVIOUS).read_text(encoding="utf-8"), "# Прежний\n")
        properties = catalog.properties(self.folder)
        self.assertEqual(properties.get("модель_протокола"), "Claude Opus 5.5")
        self.assertIsNone(properties.get("стоимость_протокола"))  # own subscription: the program spent nothing
        self.assertEqual(result["протокол"], "1 решение")

    def test_empty_and_unrecognized(self):
        with self.assertRaises(ToolError):
            ai_tools.TOOLS["save_protocol"].action(meeting=self.folder.name, text="  ")
        (self.recordings / "2026-10-21_10-00").mkdir()
        (self.recordings / "2026-10-21_10-00" / "микрофон.flac").write_bytes(b"flac")  # recorded but not recognized
        with self.assertRaisesRegex(ToolError, "не распознана"):
            ai_tools.TOOLS["get_protocol_task"].action(meeting="2026-10-21_10-00")

    def test_request_to_assistant(self):
        request = minutes.assistant_request(self.folder)
        self.assertIn("«Загрузка остатков»", request)
        self.assertIn(self.folder.name, request)


class TestMcpPrompt(unittest.TestCase):
    def _answer(self, method: str, **params: object) -> dict:
        return reply(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            ai_tools.TOOLS,
            ai_tools.INSTRUCTION,
            ai_tools.PROMPTS,
        )["result"]

    def test_ready_request_in_client_menu(self):
        self.assertIn("prompts", self._answer("initialize", protocolVersion="2025-06-18")["capabilities"])
        tooltip = self._answer("prompts/list")["prompts"][0]
        self.assertEqual((tooltip["name"], tooltip["arguments"][0]["name"]), ("compose_protocol", "meeting"))
        text = self._answer("prompts/get", name="compose_protocol", arguments={"meeting": "2026-10-20_10-00"})[
            "messages"
        ][0]["content"]["text"]
        self.assertIn("2026-10-20_10-00", text)
        self.assertIn("get_protocol_task", text)
        self.assertIn("save_protocol", text)
        json.dumps(text)


class TestAssistantSettings(unittest.TestCase):
    def test_assistant_is_not_a_preset(self):
        """The assistant works next to the minutes service: no preset instead of it, the hint is always there."""
        with (
            mock.patch.object(settings, "address_key", return_value=""),
            mock.patch.object(providers, "models", return_value=[]),
        ):
            window = settings_window.SettingsWindow(settings.Settings())
        self.addCleanup(window.deleteLater)
        block = window.connections["протоколы"]
        self.assertEqual(block.preset.findData("помощник"), -1)
        self.assertTrue(any(block._form.isRowVisible(line) for line in block._network_rows))
        self.assertFalse(window.minutes.about_assistant.isHidden())


class TestMinutesPanel(unittest.TestCase):
    def test_long_caption_under_button_is_not_cut(self):
        text = (
            "The transcript will be sent to OpenRouter, Claude Sonnet 5.5. You can enter your name instead of "
            '"Me" in Settings, Minutes section.'
        )
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            transcript.save(
                folder,
                [{"начало": 1.0, "конец": 2.0, "кто": "Я", "текст": "Начнем."}],
                {"длительность": 60, "провайдер": "тест", "стоимость": 0},
                [],
            )
            panel = MinutesView(folder, lambda: text)
            self.addCleanup(panel.deleteLater)
            panel.resize(900, 600)
            panel.show()
            program.processEvents()
            caption = panel._destination
            self.assertEqual(caption.text(), text)
            self.assertGreaterEqual(caption.height(), caption.heightForWidth(caption.width()))

    def test_link_under_write_button_asks_the_assistant(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            transcript.save(
                folder,
                [{"начало": 1.0, "конец": 2.0, "кто": "Я", "текст": "Начнем."}],
                {"длительность": 60, "провайдер": "тест", "стоимость": 0},
                [],
            )
            panel = MinutesView(folder, lambda: "")
            self.addCleanup(panel.deleteLater)
            asked: list[int] = []
            panel.ask_assistant.connect(lambda: asked.append(1))
            panel._assistant_link.linkActivated.emit("#")
            self.assertEqual(asked, [1])

    def test_minutes_from_assistant_appear_by_themselves(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            panel = MinutesView(folder, lambda: "")
            self.addCleanup(panel.deleteLater)
            (folder / minutes.FILE).write_text("# Итоги\n\n## Решения\n\n1. Цена из прайса.\n", encoding="utf-8")
            panel._file_changed(str(folder))
            self.assertIs(panel.pages.currentIndex(), 2)  # viewing the ready minutes


if __name__ == "__main__":
    unittest.main()
