# SPDX-License-Identifier: GPL-3.0-only
"""The recording notepad with the transcript during the meeting: the "Now" section with the latest phrases and the
recording minute, a click puts a phrase into the notes with its minute, a question goes in the background ("Asking…")
and its answer links to the phrases; all of it is inside the notepad, hidden from screen sharing. The setting is off by
default and unavailable with cloud recognition."""

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from knotes import failures, live, notes, providers, recognition
from knotes import settings
from knotes.language import tr
from knotes.ui import notepad as notepad_module
from knotes.ui import settings_window, style
from knotes.ui.notepad import NotepadWindow

app = QApplication.instance() or QApplication([])


class TestLivePanel(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name) / "2026-10-26_10-00"
        self.folder.mkdir()
        self.flow = live.Live(
            self.folder, recognition.Params(providers.find(providers.LOCAL), ""), lambda: 600.0, settings.Settings()
        )
        self.flow.others_heard, self.flow.done_until = True, 70.0
        self.flow._phrases = [  # noqa: SLF001 - as if two pieces were already recognized
            live.Phrase(6.0, 9.0, "собеседники", "Пришлем смету до пятницы."),
            live.Phrase(63.0, 65.0, "микрофон", "Хорошо, жду."),
        ]
        self.flow.version = 2
        self.jobs: list[tuple] = []
        patcher = mock.patch.object(notepad_module, "run_in_background", lambda *job: self.jobs.append(job))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        app.processEvents()
        self._tmp.cleanup()

    def _window(self, live_: live.Live | None = None) -> NotepadWindow:
        window = NotepadWindow(self.folder, True, [], lambda: 120.0, live_)
        self.addCleanup(app.processEvents)
        self.addCleanup(window.deleteLater)
        window.show()
        return window

    def test_without_live_transcript_no_section(self):
        self.assertIsNone(self._window().live_panel)

    def test_latest_phrases_with_minute_inside_the_hidden_notepad(self):
        window = self._window(self.flow)
        panel = window.live_panel
        self.assertIs(panel.window(), window)  # not a window of its own: hidden from screen sharing with the notepad
        self.assertTrue(window.property(style.HIDDEN_FROM_CAPTURE))
        rows = [panel.list_.item(n).text() for n in range(panel.list_.count())]
        self.assertEqual(rows, ["00:06  Собеседники: Пришлем смету до пятницы.", "01:03  Я: Хорошо, жду."])
        self.assertEqual(panel.status.text(), tr("Recognized up to {time}", time="01:10"))
        self.flow._phrases.append(live.Phrase(80.0, 82.0, "собеседники", "И акт."))  # noqa: SLF001
        self.flow.version += 1
        window.refresh(90.0)
        self.assertEqual(panel.list_.count(), 3)
        self.flow.error = "Модель «GigaAM» повреждена."
        window.refresh(91.0)
        self.assertEqual(panel.status.text(), "Модель «GigaAM» повреждена.")

    def test_click_puts_phrase_into_notes_with_its_minute(self):
        window = self._window(self.flow)
        window.notepad.editor.setPlainText("моя заметка")
        window.live_panel.list_.itemClicked.emit(window.live_panel.list_.item(0))
        window.notepad.save()
        self.assertEqual(
            [(s.second, s.text) for s in notes.read(self.folder).lines][-1],
            (6, "Собеседники: Пришлем смету до пятницы."),
        )

    def test_question_in_background_answer_with_links(self):
        window = self._window(self.flow)
        panel = window.live_panel
        panel.question.setText("что клиент обещал прислать?")
        panel.ask()
        self.assertEqual(panel.ask_button.text(), tr("Asking…"))
        self.assertFalse(panel.ask_button.isEnabled())
        panel.ask()  # a second press while the first question runs does nothing
        self.assertEqual(len(self.jobs), 1)
        _pool, work, on_result = self.jobs[0]
        with mock.patch.object(self.flow, "ask", return_value="Смету до пятницы [00:06].") as ask:
            on_result(work())
        ask.assert_called_once_with("что клиент обещал прислать?")
        self.assertEqual(panel.ask_button.text(), tr("Ask"))
        self.assertTrue(panel.ask_button.isEnabled())
        self.assertIn('<a href="6" style="color: #9FC5F0">[00:06]</a>', panel.answer.text())
        self.assertTrue(panel.answer.isVisibleTo(window))
        panel.answer.linkActivated.emit("63")
        self.assertEqual(panel.list_.currentRow(), 1)

    def test_failed_question_in_plain_text(self):
        window = self._window(self.flow)
        panel = window.live_panel
        panel.question.setText("вопрос?")
        panel.question.returnPressed.emit()
        error = providers.ProviderError("OpenRouter отказал: нет денег")
        self.jobs[0][2](error)
        self.assertIn(failures.text(error), panel.answer.text())
        self.assertEqual(panel.answer.objectName(), "ошибка_текст")
        panel.question.clear()
        panel.ask()  # an empty question is not sent
        self.assertEqual(len(self.jobs), 1)


class TestSetting(unittest.TestCase):
    def setUp(self):
        for patcher in (
            mock.patch.object(settings, "key", return_value=""),
            mock.patch.object(settings, "address_key", return_value=""),
            mock.patch.object(providers, "models", return_value=[]),
        ):  # no credential store, no network
            patcher.start()
            self.addCleanup(patcher.stop)

    def _window(self, config: settings.Settings) -> settings_window.SettingsWindow:
        window = settings_window.SettingsWindow(config)
        self.addCleanup(window.deleteLater)
        return window

    def test_off_by_default_and_stored_in_russian(self):
        self.assertFalse(settings.Settings().live_transcript)
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "настройки.json"
            settings.save(settings.Settings(live_transcript=True), path)
            self.assertIs(json.loads(path.read_text(encoding="utf-8"))["расшифровка_во_время_встречи"], True)
            self.assertTrue(settings.load(path).live_transcript)

    def test_switch_in_speech_recognition_unavailable_in_the_cloud(self):
        window, changes = self._window(settings.Settings()), []
        window.changed.connect(changes.append)
        self.assertFalse(window.recognition.live_switch.isChecked())
        self.assertTrue(window.recognition.live_switch.isEnabled())
        self.assertTrue(window.recognition.live_note.isHidden())
        window.recognition.live_switch.click()
        self.assertTrue(changes[-1].live_transcript)
        cloud = self._window(settings.Settings(provider="openrouter", live_transcript=True)).recognition
        self.assertFalse(cloud.live_switch.isEnabled())  # not paid twice: the cloud recognizes after the meeting
        self.assertFalse(cloud.live_note.isHidden())
        self.assertEqual(cloud.live_note.text(), tr(live.CLOUD))


if __name__ == "__main__":
    unittest.main()
