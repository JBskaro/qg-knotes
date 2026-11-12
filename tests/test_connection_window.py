# SPDX-License-Identifier: GPL-3.0-only
"""Connection in settings: a preset fills in the address and model, the address is trimmed to /v1, one server shares
the key, "Check" shows how many models the service has; failures are clear texts, a late answer for another preset
is dropped, a key not saved says so."""

import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFormLayout, QWidget

from knotes import minutes, providers
from knotes import settings
from knotes.ui import settings_window
from knotes.ui import connection

app = QApplication.instance() or QApplication([])


def _immediately(_pool: object, work, on_result) -> None:
    on_result(work())


def _caught(_pool: object, work, on_result) -> None:
    """Like the real queue: a failure of the work comes to on_result."""
    try:
        result = work()
    except Exception as error:  # noqa: BLE001 - the real queue catches the same way
        result = error
    on_result(result)


class TestConnectionBlock(unittest.TestCase):
    def setUp(self):
        for patcher in (
            mock.patch.object(connection, "run_in_background", _immediately),
            mock.patch.object(providers, "models", return_value=[("qwen3:14b", "qwen3:14b")]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.page = QWidget()
        self.form_layout = QFormLayout(self.page)
        self.changes: dict = {}

    def _block(self, function: str, config: settings.Settings, other: str = "") -> connection.ConnectionBlock:
        block = connection.ConnectionBlock(function, config, ("Распознавание речи", lambda: other), self.page)
        block.edited.connect(self.changes.update)
        block.into_form(self.form_layout)
        return block

    def test_preset_fills_address_and_model(self):
        block = self._block("протоколы", settings.Settings())
        self.assertEqual(block.address.text(), "https://openrouter.ai/api/v1")
        self.assertIn("Claude Sonnet 5.5", block.model.currentText())
        block.preset.setCurrentIndex(block.preset.findData("свой"))
        self.assertEqual(self.changes, {"minutes_service": "свой", "minutes_address": "", "minutes_model_id": ""})
        self.assertEqual(block.address.text(), "http://localhost:11434/v1")
        self.assertEqual(block.model.itemData(0), "qwen3:14b")  # the model list comes from the server itself

    def test_own_server_named_by_its_api(self):
        """The own server preset names the API it speaks; it is the user's server, so there is no "(not verified)"."""
        block = self._block("распознавание", settings.Settings())
        self.assertEqual(block.preset.itemText(block.preset.findData("свой")), "Свой сервер (OpenAI API)")

    def test_own_address_and_shared_key(self):
        block = self._block("протоколы", settings.Settings(), other="https://openrouter.ai/api/v1")
        self.assertFalse(block.shared_label.isHidden())
        self.assertIn("Распознавание речи", block.shared_label.text())
        block.address.setText(" https://мой.сервер/v1/chat/completions ")
        block.address.editingFinished.emit()
        self.assertEqual(self.changes["minutes_address"], "https://мой.сервер/v1")
        self.assertEqual(block.address.text(), "https://мой.сервер/v1")
        self.assertTrue(block.shared_label.isHidden())
        block.address.setText("https://openrouter.ai/api/v1")  # the preset address: empty in the settings again
        block.address.editingFinished.emit()
        self.assertEqual(self.changes["minutes_address"], "")

    def test_check_shows_model_count(self):
        block = self._block("протоколы", settings.Settings(minutes_service="свой"))
        with mock.patch.object(providers, "check_key", return_value="") as checking:
            block.secret.check.click()
        self.assertEqual(checking.call_args.args[0].address, "http://localhost:11434/v1")
        self.assertEqual(block.secret.result.text(), "Сервер отвечает, моделей: 1")
        with mock.patch.object(providers, "check_key", return_value="Сервер не принял ключ"):
            block.secret.check.click()
        self.assertEqual(block.secret.result.text(), "Сервер не принял ключ")

    def test_settings_window_with_two_connections(self):
        """Both sections on one server: the shared key note in both; the minutes server changed: no note."""
        window = settings_window.SettingsWindow(settings.Settings(provider="openrouter"))
        self.addCleanup(window.deleteLater)
        speech, minutes_list = window.connections["распознавание"], window.connections["протоколы"]
        self.assertFalse(speech.shared_label.isHidden() or minutes_list.shared_label.isHidden())
        minutes_list.preset.setCurrentIndex(minutes_list.preset.findData("lmstudio"))
        self.assertTrue(speech.shared_label.isHidden())
        self.assertEqual((window.settings.minutes_service, window.settings.provider), ("lmstudio", "openrouter"))

    def test_preset_model_empty_in_settings(self):
        block = self._block("распознавание", settings.Settings(provider="openrouter"))
        block.model.setEditText("whisper-1")
        block.model.lineEdit().editingFinished.emit()
        self.assertEqual(self.changes["model"], "whisper-1")
        block.model.setEditText("elevenlabs/scribe-v2")
        block.model.lineEdit().editingFinished.emit()
        self.assertEqual(self.changes["model"], "")

    def test_manual_minutes_model_survives_a_late_list_and_reaches_request(self):
        window = settings_window.SettingsWindow(settings.Settings(minutes_service="openrouter"))
        self.addCleanup(window.deleteLater)
        window.show_section("протоколы")
        block = window.connections["протоколы"]
        field = block.model.lineEdit()
        field.selectAll()
        custom = "publisher/model-preview:free"
        QTest.keyClicks(field, custom)
        self.assertEqual(window.settings.minutes_model_id, "")
        block._show_models([("publisher/other-model", "Other model")])
        self.assertEqual(field.text(), custom)
        QTest.keyClick(field, Qt.Key.Key_Return)
        self.assertEqual(window.settings.minutes_model_id, custom)
        with mock.patch.object(minutes, "instruction", return_value="test"):
            params = minutes.params(window.settings)
        self.assertEqual(params.model, custom)
        self.assertEqual(params.service.id, "openrouter")

    def test_model_list_without_id_is_said_clearly(self):
        block = self._block("протоколы", settings.Settings(minutes_service="свой"))
        with (
            mock.patch.object(providers, "models", side_effect=KeyError("id")),
            mock.patch.object(connection, "run_in_background", _caught),
        ):
            block._load_models()
        self.assertIn("впишите название модели вручную", block.secret.result.text())
        self.assertNotIn("'id'", block.secret.result.text())
        with (
            mock.patch.object(providers, "check_key", return_value=""),
            mock.patch.object(providers, "models", side_effect=KeyError("id")),
        ):
            block.secret.check.click()  # the key is accepted, the list is not there: the check passed
        self.assertEqual(block.secret.result.text(), "Сервер отвечает")

    def test_key_refusal_logged_without_key(self):
        block = self._block("протоколы", settings.Settings())
        block.secret.field.setText("sk-or-v1-секрет")
        with (
            mock.patch.object(providers, "check_key", return_value="Ключ не подошел"),
            mock.patch.object(settings, "set_address_key"),
            self.assertLogs("knotes.ui.connection", "WARNING") as logged,
        ):
            block.secret.check.click()
        self.assertIn("openrouter.ai", logged.output[0])
        self.assertNotIn("секрет", "".join(logged.output))

    def test_answer_for_earlier_preset_is_dropped(self):
        later: list = []
        with mock.patch.object(connection, "run_in_background", lambda _pool, work, done: later.append((work, done))):
            block = self._block("протоколы", settings.Settings(minutes_service="openai"))  # asks OpenAI for models
            block.preset.setCurrentIndex(block.preset.findData("свой"))
        _work, done = later[0]
        done([("gpt-x", "GPT X")])  # the OpenAI list comes after the preset changed
        self.assertEqual(block.model.findData("gpt-x"), -1)
        work, done = later[-1]
        done(work())
        self.assertEqual(block.model.itemData(0), "qwen3:14b")

    def test_key_not_saved_says_so(self):
        block = self._block("протоколы", settings.Settings())
        block.secret.field.setText("sk-новый")
        with mock.patch.object(settings, "set_address_key", side_effect=OSError("store")):
            block.save_key()
        self.assertIn("не сохранил ключ", block.secret.result.text())
        self.assertTrue(block.secret.saved.isHidden())

    def test_key_not_saved_is_not_checked(self):
        """A key the store refused is not checked: the line keeps saying why, meetings would not get this key."""
        block = self._block("протоколы", settings.Settings())
        block.secret.field.setText("sk-новый")
        with (
            mock.patch.object(settings, "set_address_key", side_effect=OSError("store")),
            mock.patch.object(providers, "check_key", return_value="") as checking,
        ):
            block.secret.check.click()
        checking.assert_not_called()
        self.assertIn("не сохранил ключ", block.secret.result.text())


if __name__ == "__main__":
    unittest.main()
