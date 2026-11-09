# SPDX-License-Identifier: GPL-3.0-only
"""The "Choose a model" window and the local speech recognition rows in settings: models from the site, download,
choice and keeping it when the preset changes, deletion, partial downloads, language, mirror, link, a late list
answer, sizes, closing and exiting mid-download, downloading diarization models when the switch is turned on."""

import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton, QRadioButton

from knotes import models, providers
from knotes import settings
from knotes.ui import models_window
from knotes.ui import settings_calendar, settings_window
from knotes.ui import setup_wizard

program = QApplication.instance() or QApplication([])


def _immediately(_pool: object, work, on_result) -> None:
    try:
        result = work()
    except Exception as error:  # noqa: BLE001 - the real queue catches the same way
        result = error
    on_result(result)


def _download(folder: pathlib.Path, model: models.Model) -> None:
    """A model as after download: files of the right size and the marker."""
    place = folder / model.id
    place.mkdir(parents=True, exist_ok=True)
    for file in model.files:
        (place / file.name).write_bytes(b"\0" * file.size)
    models._write_json(place / models.MARKER, {"файлы": {f.name: {"размер": f.size} for f in model.files}})


def _download_all(folder: pathlib.Path, list_: list, progress=None, *_: object) -> None:
    for model in list_:
        _download(folder, model)
    if progress:
        progress(8, 16)


def _lines(section) -> list[tuple[str, list[str]]]:
    """Rows of a models window section: the heading text (radio button or title) and the button captions."""
    result = []
    for number in range(section.count()):
        line = section.itemAt(number).widget()
        if line is None or not line.findChildren(QPushButton):
            continue
        captions = [*line.findChildren(QRadioButton), *line.findChildren(QLabel)]
        result.append(
            (next(v.text() for v in captions if v.text()), [k.text() for k in line.findChildren(QPushButton)])
        )
    return result


def _press(section, title: str, button: str) -> None:
    for number in range(section.count()):
        line = section.itemAt(number).widget()
        if line is not None and any(
            v.text() == title for v in [*line.findChildren(QRadioButton), *line.findChildren(QLabel)]
        ):
            next(k for k in line.findChildren(QPushButton) if k.text() == button).click()
            return
    raise AssertionError(f"нет строки {title}")


class TestModelsWindow(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name)
        files = [{"rfilename": i} for i in ("encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx", "tokens.txt")]
        self.from_site = models.hf_model(
            {"id": "csukuangfj/sherpa-onnx-zipformer-ru-int8-2025-04-20", "sha": "c" * 40, "siblings": files}
        )
        self.full_one = models.dataclasses.replace(
            self.from_site, files=tuple(models.dataclasses.replace(f, size=2**18) for f in self.from_site.files)
        )
        # catalog with tiny files: a "downloaded" model matches its marker
        self.catalog = [
            models.dataclasses.replace(m, files=tuple(models.dataclasses.replace(f, size=16) for f in m.files))
            for m in models.catalog()
        ]
        for patcher in (
            mock.patch.object(settings, "key", return_value=""),
            mock.patch.object(settings, "set_key"),
            mock.patch.object(settings, "address_key", return_value=""),
            mock.patch.object(settings, "set_address_key"),
            mock.patch.object(settings, "models_folder", return_value=self.folder),
            mock.patch.object(settings_calendar, "run_in_background", _immediately),
            mock.patch.object(setup_wizard, "run_in_background", _immediately),  # the downloads of the settings
            mock.patch.object(models_window, "run_in_background", _immediately),
            mock.patch.object(models_window.QDialog, "exec"),
            mock.patch.object(models, "catalog", lambda: self.catalog),
            mock.patch.object(models, "from_site", return_value=[self.from_site]),
            mock.patch.object(models, "read", return_value=self.full_one),
            mock.patch.object(models, "download_all", side_effect=_download_all),
            mock.patch.object(providers, "models", return_value=[]),
        ):  # cloud model list without network
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def window(
        self, language: str = "ru", selected_one: str = "gigaam-v3-ctc-punct-int8"
    ) -> models_window.ModelsWindow:
        window = models_window.ModelsWindow(self.folder, "", language)
        self.addCleanup(window.deleteLater)
        window.open_view(selected_one, False)
        return window

    def test_download_from_site_choose_delete(self):
        window = self.window()
        chosen_items, changes = [], []
        window.chosen.connect(chosen_items.append)
        window.edited.connect(lambda: changes.append(1))
        self.assertEqual(_lines(window._here), [])
        self.assertEqual([z for z, _ in _lines(window._download)], ["GigaAM v3", "Whisper large-v3-turbo", "Zipformer"])
        self.assertEqual(window._descriptions[self.from_site.id].text(), "Русский · 1 МБ")  # the size came separately
        window._search.setText("zipformer")
        window._typing.timeout.emit()  # typing pause
        self.assertEqual(_lines(window._download), [("Zipformer", ["Скачать"])])
        _press(window._download, "Zipformer", "Скачать")
        needed_models = models.download_all.call_args.args[1]
        self.assertEqual(
            [m.id for m in needed_models], ["silero-vad", self.from_site.id]
        )  # the helper model comes with the model
        self.assertEqual(models.custom_items(self.folder), [self.full_one])  # the site model went into custom models
        self.assertEqual(chosen_items, [self.from_site.id])  # chosen one not downloaded: the downloaded one is chosen
        self.assertEqual((_lines(window._here), changes), ([("Zipformer", ["Удалить"])], [1]))
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            _press(window._here, "Zipformer", "Удалить")
        self.assertEqual((_lines(window._here), changes, chosen_items), ([], [1, 1], [self.from_site.id, ""]))

    def test_deleted_chosen_picks_other_downloaded(self):
        for model in self.catalog:
            _download(self.folder, model)
        window = self.window(selected_one="whisper-large-v3-turbo-int8")
        chosen_items = []
        window.chosen.connect(chosen_items.append)
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            _press(window._here, "Whisper large-v3-turbo", "Удалить")
        self.assertEqual(chosen_items, ["gigaam-v3-ctc-punct-int8"])

    def test_partial_resume_or_delete(self):
        (self.folder / "whisper-large-v3-turbo-int8").mkdir()
        (self.folder / "whisper-large-v3-turbo-int8" / "turbo-encoder.int8.onnx.part").write_bytes(b"1")
        window = self.window()
        self.assertIn(("Whisper large-v3-turbo", ["Докачать", "Удалить"]), _lines(window._download))

    def test_one_model_cannot_download_twice(self):
        window = self.window()
        with mock.patch.object(models_window, "run_in_background") as background_:
            window._start(self.from_site)
            window._start(self.from_site)
        self.assertEqual(background_.call_count, 1)
        self.assertEqual(_lines(window._download)[-1], ("Zipformer", ["Отмена"]))

    def test_language_mirror_and_link(self):
        window = self.window("en", "")
        self.assertEqual([z for z, _ in _lines(window._download)], ["Whisper large-v3-turbo"])  # Russian hidden
        mirrors = []
        window.mirror_changed.connect(mirrors.append)
        with (
            mock.patch.object(models_window.QInputDialog, "getText", return_value=("http://hf-mirror.com", True)),
            mock.patch.object(QMessageBox, "warning") as warning,
        ):
            window._set_mirror()
        warning.assert_called_once()  # not allowed without https
        with mock.patch.object(models_window.QInputDialog, "getText", return_value=("https://hf-mirror.com/", True)):
            window._set_mirror()
        self.assertEqual(mirrors, ["https://hf-mirror.com"])
        started = []
        with (
            mock.patch.object(window, "_start", started.append),
            mock.patch.object(models_window.QInputDialog, "getText", return_value=("o/zipformer", True)),
        ):
            window._paste_link()
        models.read.assert_called_with("o/zipformer", "https://hf-mirror.com")
        self.assertEqual(started, [self.full_one])  # the link was pasted to download

    def test_late_list_answer_and_retry_after_error(self):
        window = self.window()
        window._show_site(window._generation - 1, providers.NetworkError("нет связи"))  # answer from old mirror
        self.assertEqual(window._site_error, "")
        window._show_site(window._generation, providers.NetworkError("Нет интернета."))
        self.assertEqual((window._site_error, window._retry.isHidden()), ("Нет интернета.", False))
        window.open_view("", False)  # not loaded: requested again on open
        self.assertEqual((window._site_error, [m.id for m in window._site]), ("", [self.from_site.id]))

    def test_size_unknown_and_not_asked_again(self):
        models.read.side_effect = providers.NetworkError("нет связи")
        window = self.window()
        self.assertEqual(window._descriptions[self.from_site.id].text(), "Русский · размер не узнать")
        requests_count = models.read.call_count
        window._search.setText("zip")
        window._typing.timeout.emit()
        self.assertEqual(models.read.call_count, requests_count)

    def test_buttons_not_default(self):
        """Enter in the search field presses no window button (the language segments are buttons too)."""
        window = self.window()
        self.assertFalse(any(k.autoDefault() for k in window.findChildren(QPushButton)))

    def test_close_during_download_and_exit(self):
        window = self.window()
        cancel = window._cancels["x"] = threading.Event()
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            window.done(0)
        self.assertFalse(cancel.is_set())
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            window.done(0)
        self.assertTrue(cancel.is_set())  # .part stays for resuming
        another = window._cancels["y"] = threading.Event()
        window._stop_all()  # exiting the program
        self.assertTrue(another.is_set())

    def test_settings_model_row_and_choice_kept(self):
        window = settings_window.SettingsWindow(settings.Settings(provider=providers.LOCAL))
        self.addCleanup(window.deleteLater)
        speech, form_layout = window.connections["распознавание"], window.recognition.form_layout
        local_model = window.recognition.local_model
        self.assertEqual(
            [form_layout.isRowVisible(s) for s in [*speech._network_rows, *local_model.rows]],
            [False, False, False, True, True],
        )
        self.assertEqual(local_model.model_label.text(), "GigaAM v3 · не скачана")
        _download(self.folder, self.catalog[1])
        speech.set_model("whisper-large-v3-turbo-int8")
        self.assertEqual(window.settings.local_model, "whisper-large-v3-turbo-int8")
        speech.preset.setCurrentIndex(speech.preset.findData("openrouter"))  # cloud: address, API key, model again
        self.assertEqual(
            [form_layout.isRowVisible(s) for s in [*speech._network_rows, *local_model.rows]],
            [True, True, True, False, False],
        )
        speech.preset.setCurrentIndex(speech.preset.findData(providers.LOCAL))
        self.assertEqual(speech.chosen_model, "whisper-large-v3-turbo-int8")  # changing the preset did not erase it
        self.assertTrue(local_model.model_label.text().startswith("Whisper large-v3-turbo · Много языков"))

    def test_settings_download_and_choose_model(self):
        """The "Download" button downloads the chosen model at once, "Choose a model…" opens the search window and
        downloads nothing."""
        window = settings_window.SettingsWindow(settings.Settings(provider=providers.LOCAL, speaker_diarization=False))
        self.addCleanup(window.deleteLater)
        local_model = window.recognition.local_model
        self.assertEqual(local_model.switch_to.text(), "Выбрать модель…")
        self.assertFalse(local_model.download.isHidden())
        local_model.switch_to.click()
        models.download_all.assert_not_called()
        local_model.download.click()
        self.assertIn("gigaam-v3-ctc-punct-int8", [m.id for m in models.download_all.call_args.args[1]])
        self.assertEqual(local_model.model_label.text().split(" · ")[0], "GigaAM v3")
        self.assertTrue(local_model.download.isHidden())  # downloaded: only choosing another one is left

    def test_enabled_diarization_downloads_models_at_once(self):
        window = settings_window.SettingsWindow(settings.Settings(provider=providers.LOCAL, speaker_diarization=False))
        self.addCleanup(window.deleteLater)
        local_model = window.recognition.local_model
        local_model.enable_diarization(True)
        self.assertEqual(
            [m.id for m in models.download_all.call_args.args[1]],  # and the pause splitter, if it is missing
            ["silero-vad", "pyannote-segmentation-3-0", "wespeaker-voxceleb-resnet34"],
        )
        self.assertTrue(local_model.diarization_status.isHidden())  # downloaded: the progress row is gone
        models.download_all.reset_mock()
        local_model.enable_diarization(True)
        models.download_all.assert_not_called()  # already downloaded


if __name__ == "__main__":
    unittest.main()
