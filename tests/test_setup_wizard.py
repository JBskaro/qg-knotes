# SPDX-License-Identifier: GPL-3.0-only
"""First-run setup: the levels show whether the microphone and the participants' sound are heard, the device choice
is saved, the recognition choice and its key are saved and the model starts downloading, the test recording shows
its text or a clear error, skipping and finishing say so, the temporary folders go away. Sound, recording,
downloading and recognition are stand-ins: nothing plays, nothing is recorded or sent."""

import pathlib
import time
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from knotes import recording, models, player, providers, language
from knotes import settings
from knotes.ui import setup_wizard as wizard

program = QApplication.instance() or QApplication([])
LOUD = 12000  # track peak: the bars are more than half full


class FakeRecording:
    """Stands in for a recording: levels set by the test, the end comes when the test says so."""

    all_: list["FakeRecording"] = []
    level = {"микрофон": 0, "собеседники": 0}

    def __init__(
        self, folder: pathlib.Path, events: recording.Events | None = None, devices=("", ""), screen_capture=None
    ):
        self.folder, self.events, self.devices = folder, events or recording.Events(), devices
        self.running, self.stopped = False, False
        self.all_.append(self)

    def start_now(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running, self.stopped = False, True

    def wait(self, _seconds: float | None = None) -> None:
        pass

    def levels(self) -> dict[str, int]:
        return dict(self.level)

    def finish(self, error: str = "") -> None:
        self.events.ended(self.folder, error)


def _immediately(_pool: object, work, on_result) -> None:
    try:
        result = work()
    except Exception as error:  # noqa: BLE001 - the real queue catches the same way
        result = error
    on_result(result)


def _gone(folder: pathlib.Path) -> bool:
    """The temporary folder goes in the background: wait for it a little."""
    for _ in range(100):
        if not folder.exists():
            return True
        time.sleep(0.02)
    return False


class TestWizard(unittest.TestCase):
    def setUp(self):
        FakeRecording.all_, FakeRecording.level = [], {"микрофон": 0, "собеседники": 0}
        self.keys: dict[str, str] = {}
        self.downloaded_count: list[list[str]] = []
        for patcher in (
            mock.patch.object(recording, "Recording", FakeRecording),
            mock.patch.object(recording, "device_list", return_value=(["Гарнитура"], ["Наушники"])),
            mock.patch.object(player, "chime"),
            mock.patch.object(wizard, "run_in_background", _immediately),
            mock.patch.object(settings, "set_address_key", self.keys.__setitem__),
            mock.patch.object(settings, "address_key", return_value=""),
            mock.patch.object(
                models,
                "download_all",
                lambda _p, list_, *_arg, **_to: self.downloaded_count.append([m.id for m in list_]),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.changes: list[settings.Settings] = []
        self.result: list[str] = []
        self.window = wizard.SetupWizard(settings.Settings())
        self.window.changed.connect(self.changes.append)
        self.window.closed.connect(self.result.append)
        self.window.show()
        self.addCleanup(self.window.close)

    def _recording(self) -> FakeRecording:
        return FakeRecording.all_[-1]

    def test_sound_heard_and_device_choice(self):
        self.assertEqual(self.window._title.text(), "Проверим звук")
        self.assertTrue(self._recording().running)
        self.assertNotIn("Слышно", self.window._mic_state.text())
        FakeRecording.level = {"микрофон": LOUD, "собеседники": 0}
        self.window._tick()
        self.assertIn("Слышно", self.window._mic_state.text())
        self.assertNotIn("Слышно", self.window._out_state.text())
        self.window._check.click()
        player.chime.assert_called_once_with("")
        first = self._recording()
        self.window._mic.setCurrentIndex(self.window._mic.findData("Гарнитура"))
        self.assertTrue(first.stopped)  # the check restarted with the new microphone
        self.assertEqual(self._recording().devices, ("Гарнитура", ""))
        self.assertEqual(self.changes[-1].microphone, "Гарнитура")

    def test_service_with_key(self):
        self.window._next.click()
        self.assertTrue(self._recording().stopped)  # levels are not recorded on the second step
        self.assertEqual(self.window._title.text(), "Чем распознавать речь")
        self.window._service.click()
        self.window._preset.setCurrentIndex(self.window._preset.findData("openrouter"))
        self.window._key.setText(" sk-test ")
        self.window._next.click()
        self.assertEqual(self.changes[-1].provider, "openrouter")
        self.assertEqual(self.keys, {"https://openrouter.ai/api/v1": "sk-test"})
        self.assertEqual(self.downloaded_count, [])
        self.assertEqual(self.window._title.text(), "Пробная запись")

    def test_local_model_downloads(self):
        self.window._next.click()
        self.assertTrue(self.window._local.isChecked())  # selected right away on a new install
        self.window._next.click()
        self.assertEqual(self.changes[-1].provider, providers.LOCAL)
        self.assertIn("gigaam-v3-ctc-punct-int8", self.downloaded_count[0])

    def _to_test_step(self) -> None:
        self.window._next.click()
        self.window._next.click()

    def test_test_recording_shows_text(self):
        self._to_test_step()
        with mock.patch.object(
            wizard, "transcribe", return_value=[{"кто": "Я", "текст": "Проверка связи."}]
        ) as recognize:
            self.window._record.click()
            probe = self._recording()
            self.assertTrue(probe.running)
            self.window._record.click()  # "Stop" before 10 seconds
            probe.finish()
        self.assertFalse(recognize.call_args.args[1].diarization)  # test recording: no diarization, faster
        self.assertIn("Проверка связи.", self.window._result.text())
        self.assertIn("работает", self.window._state.text())
        self.window._next.click()
        self.assertEqual(self.result, ["done"])
        self.assertTrue(_gone(probe.folder))

    def test_test_recording_silence_and_error(self):
        self._to_test_step()
        with mock.patch.object(wizard, "transcribe", return_value=[]):
            self.window._record.click()
            self.window._record.click()
            self._recording().finish()
        self.assertIn("тишина", self.window._state.text())
        failure = providers.ProviderError("Ключ не подошел")
        with mock.patch.object(wizard, "transcribe", side_effect=failure):
            self.window._record.click()
            self.window._record.click()
            self._recording().finish()
        self.assertIn("Ключ не подошел", self.window._state.text())

    def test_no_microphone(self):
        with mock.patch.object(recording, "device_list", return_value=([], ["Наушники"])):
            window = wizard.SetupWizard(settings.Settings())
        self.addCleanup(window.close)
        window._tick()
        self.assertIn("Микрофон не найден", window._mic_state.text())

    def test_skip(self):
        folder = self._recording().folder
        self.window._skip.click()
        self.assertEqual(self.result, ["skipped"])
        self.assertTrue(self._recording().stopped)
        self.assertTrue(_gone(folder))  # removed in the background, the window does not wait

    def test_device_list_as_in_settings(self):
        window = wizard.SetupWizard(settings.Settings(microphone="USB-микрофон"))
        self.addCleanup(window.close)
        self.assertEqual(window._mic.itemText(0), "Как в Windows")
        self.assertEqual(window._mic.currentText(), "USB-микрофон (сейчас не подключено)")  # the choice is not lost

    def test_sound_check_says_what_is_wrong_with_devices(self):
        events = self._recording().events
        events.change_item("Выбранный микрофон «USB» не найден, пишу микрофон, выбранный в Windows")
        program.processEvents()
        self.assertIn("«USB» не найден", self.window._mic_state.text())
        self.window._tick()  # the levels do not hide the note
        self.assertIn("«USB» не найден", self.window._mic_state.text())
        events.ended(self._recording().folder, language.tr(wizard.NO_OUTPUT))
        program.processEvents()
        self.assertIn("Не найдены наушники или колонки", self.window._out_state.text())
        self.assertEqual(self.window._out_state.objectName(), "ошибка_текст")

    def test_sound_check_stops_without_waiting_for_its_sound(self):
        probe = self.window._probe
        (probe.folder / recording.MANIFEST).write_text("[]", encoding="utf-8")
        with mock.patch.object(FakeRecording, "wait", lambda _self, _s=None: time.sleep(0.5)):
            started = time.monotonic()
            self.window._next.click()
            self.assertLess(time.monotonic() - started, 0.3)
        self.assertFalse((probe.folder / recording.MANIFEST).exists())  # nothing to assemble
        self.assertTrue(_gone(probe.folder))


class TestModelDownload(unittest.TestCase):
    """The model download outlives the setup window; the setup waits for it only for recognition on this computer."""

    def setUp(self):
        self.jobs: list = []
        self.downloads: list[list[str]] = []
        self.told: list[tuple[str, str]] = []
        for patcher in (
            mock.patch.object(recording, "Recording", FakeRecording),
            mock.patch.object(recording, "device_list", return_value=(["Гарнитура"], ["Наушники"])),
            mock.patch.object(wizard, "run_in_background", lambda _pool, work, done: self.jobs.append((work, done))),
            mock.patch.object(settings, "set_address_key"),
            mock.patch.object(settings, "address_key", return_value=""),
            mock.patch.object(
                models, "download_all", lambda _p, list_, *_a: self.downloads.append([m.id for m in list_])
            ),
            mock.patch.object(wizard, "_tell", lambda heading, text: self.told.append((heading, text))),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        wizard._downloads.clear()

    def _wizard(self) -> wizard.SetupWizard:
        window = wizard.SetupWizard(settings.Settings())
        self.addCleanup(window.close)
        window._next.click()
        window._next.click()  # "On this computer": the model starts downloading
        return window

    def _finish_download(self, failure: Exception | None = None) -> None:
        work, done = next((w, d) for w, d in self.jobs if w.__code__.co_names.count("download_all"))
        work()
        done(failure)

    def test_download_goes_on_after_window_closes_and_says_so(self):
        window = self._wizard()
        self.assertTrue(window._downloading)
        again = self._wizard()  # the same model is not downloaded twice
        self.assertIs(again._job, window._job)
        window.close()
        again.close()
        self._finish_download()
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(len(self.told), 1)
        self.assertIn("Скачано", self.told[0][0])

    def test_service_does_not_wait_for_model(self):
        window = self._wizard()
        window._show(1)
        window._service.click()
        window._next.click()
        self.assertFalse(window._downloading)
        window.close()
        self._finish_download()
        self.assertEqual(len(self.told), 1)  # nobody follows the download any more: a message tells its end

    def test_download_error_shown_on_test_step_at_once_unwrapped(self):
        window = self._wizard()
        self._finish_download(providers.ProviderError("Нет связи с сервером."))
        self.assertIn("Модель не скачалась: Нет связи с сервером.", window._state.text())
        self.assertNotIn("Неожиданная", window._state.text())
        self.assertEqual(self.told, [])  # the window follows it and says it itself
        with mock.patch.object(wizard, "transcribe") as recognize:
            window._record.click()
            window._record.click()
            FakeRecording.all_[-1].finish()
        recognize.assert_not_called()
        self.assertIn("Модель не скачалась: Нет связи с сервером.", window._state.text())
        self.assertNotIn("Неожиданная", window._state.text())


if __name__ == "__main__":
    unittest.main()
