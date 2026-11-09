# SPDX-License-Identifier: GPL-3.0-only
"""Speech recognition on this computer in the program: the track goes to the engine, not to the cloud; service models
download by themselves, a model that is not downloaded is a refusal with a hint; in the settings this preset has no
address and API key but has a model line and speaker diarization; the "Choose a model" window: models from the site,
download, choice, deletion, language, mirror, link."""

import json
import pathlib
import tempfile
import unittest
from unittest import mock

import numpy as np
from PySide6.QtWidgets import QApplication

from knotes import local_engine, models, providers
from knotes import settings
from knotes import recognition

program = QApplication.instance() or QApplication([])
SR = recognition.SAMPLE_RATE


def _immediately(_pool: object, work, on_result) -> None:
    try:
        result = work()
    except Exception as error:  # noqa: BLE001 - the real queue catches it the same way
        result = error
    on_result(result)


def _download(folder: pathlib.Path, model: models.Model) -> None:
    """A model as after a download: files of the right size and the marker."""
    place = folder / model.id
    place.mkdir(parents=True, exist_ok=True)
    for file in model.files:
        (place / file.name).write_bytes(b"\0" * min(file.size, 16))
    label = {f.name: {"размер": (place / f.name).stat().st_size, "sha256": f.sha256} for f in model.files}
    (place / models.MARKER).write_text(json.dumps({"файлы": label}), encoding="utf-8")


class TestLocalRecognition(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.models, self.meeting = root / "модели", root / "встреча"
        self.meeting.mkdir()
        audio = np.zeros(4 * SR, np.float32)
        audio[: 2 * SR] = np.random.default_rng(0).uniform(-0.5, 0.5, 2 * SR)
        recognition.write_flac(audio, self.meeting / "собеседники.flac")
        self.params = recognition.Params(providers.find(providers.LOCAL), "", models_folder=self.models)
        # tiny catalog file sizes so that the "downloaded" model matches the marker
        self.catalog = [
            models.dataclasses.replace(m, files=tuple(models.dataclasses.replace(f, size=16) for f in m.files))
            for m in models.catalog()
        ]
        patcher = mock.patch.object(models, "catalog", lambda: self.catalog)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _answer(self, *_arguments: object) -> dict:
        return {
            "text": "Добрый день",
            "duration": 4.0,
            "words": [
                {"start": 0.2, "end": 0.6, "word": "Добрый", "speaker": "speaker_0"},
                {"start": 0.7, "end": 1.1, "word": "день", "speaker": "speaker_1"},
            ],
        }

    def test_track_goes_to_engine_with_diarization_and_no_cost(self):
        for model in self.catalog:
            _download(self.models, model)
        spending, progress = [], []
        with mock.patch.object(local_engine, "recognize_in_process", side_effect=self._answer) as engine:
            file = recognition.process(
                self.meeting,
                self.params,
                progress=lambda *x_: progress.append(x_),
                payment=lambda _model, how_many: spending.append(how_many),
            )
        self.assertEqual(spending, [0])  # the spending log does not write zeros
        self.assertEqual(progress, [(0, 4), (4, 4)])  # on this computer, progress is in seconds of audio
        path, model, diarization, engine_progress, cancel = engine.call_args.args
        self.assertEqual(
            (path.name, model.kind, model.id_, pathlib.Path(model.vad).parent.name),
            ("собеседники.flac", "nemo_ctc", "gigaam-v3-ctc-punct-int8", "silero-vad"),
        )
        self.assertEqual(
            {r_: pathlib.Path(path_).name for r_, path_ in model.files.items()},
            {"model": "model.int8.onnx", "tokens": "tokens.txt"},
        )  # the program computes the paths
        self.assertEqual(
            (pathlib.Path(diarization.segmented).parent.name, pathlib.Path(diarization.voice).parent.name),
            ("pyannote-segmentation-3-0", "wespeaker-voxceleb-resnet34"),
        )
        self.assertIsNotNone(engine_progress)
        self.assertIsNone(cancel)
        text = file.read_text(encoding="utf-8")
        self.assertIn("Добрый", text)
        self.assertIn("Собеседник 2", text)  # diarization told the second participant apart

    def test_microphone_with_others_without_diarization(self):
        """Diarization split the user's own voice on the microphone into many speakers: only the computer sound is
        diarized."""
        for model in self.catalog:
            _download(self.models, model)
        noise = np.random.default_rng(1).uniform(-0.5, 0.5, 4 * SR).astype(np.float32)
        recognition.write_flac(noise, self.meeting / "микрофон.flac")
        with mock.patch.object(local_engine, "recognize_in_process", side_effect=self._answer) as engine:
            recognition.process(self.meeting, self.params)
        diarization = {call_.args[0].stem: call_.args[2] is not None for call_ in engine.call_args_list}
        self.assertEqual(diarization, {"микрофон": False, "собеседники": True})

    def test_main_model_first_service_models_fetched(self):
        """The main model is not downloaded: refusal at once, no downloads; service models are small and are fetched
        by themselves in one quick attempt (the meeting does not wait for minutes)."""
        gigaam = next(m for m in self.catalog if m.id == "gigaam-v3-ctc-punct-int8")
        track = self.meeting / "собеседники.flac"
        with (
            mock.patch.object(models, "fetch_missing") as fetch_missing,
            self.assertRaisesRegex(providers.ProviderError, "не скачана"),
        ):
            recognition.recognize_track(track, 4, self.params)
        fetch_missing.assert_not_called()
        _download(self.models, gigaam)
        downloaded_all = []

        def fetch_missing_(folder: pathlib.Path, list_: list, *_: object) -> None:
            for model in list_:
                downloaded_all.append(model.id)
                _download(folder, model)

        no_diarization = models.dataclasses.replace(self.params, diarization=False)
        with (
            mock.patch.object(models, "fetch_missing", fetch_missing_),
            mock.patch.object(local_engine, "recognize_in_process", side_effect=self._answer) as engine,
        ):
            parts = recognition.recognize_track(track, 4, no_diarization)
            self.assertEqual(downloaded_all, ["silero-vad"])  # without diarization, only splitting by pauses
            self.assertIsNone(engine.call_args.args[2])
            recognition.recognize_track(track, 4, self.params)
        self.assertEqual(downloaded_all, ["silero-vad", "pyannote-segmentation-3-0", "wespeaker-voxceleb-resnet34"])
        self.assertEqual(parts[0]["ответ"]["text"], "Добрый день")

    def test_service_models_failed_plain_failure(self):
        _download(self.models, next(m for m in self.catalog if m.id == "gigaam-v3-ctc-punct-int8"))
        no_connection = models.ModelError("Нет интернета или не найден адрес huggingface.co.")
        with (
            mock.patch.object(models, "fetch_missing", side_effect=no_connection),
            self.assertLogs(recognition.log, "WARNING"),
            self.assertRaises(providers.ProviderError) as error,
        ):
            recognition.recognize_track(self.meeting / "собеседники.flac", 4, self.params)
        self.assertIn("один раз нужны служебные модели", str(error.exception))
        self.assertIn("Нет интернета", str(error.exception))

    def test_local_model_own_field(self):
        """Switching the preset to a cloud one and back keeps the local model choice."""
        config = settings.Settings(provider=providers.LOCAL, model="облачная", local_model="whisper")
        with mock.patch.object(settings, "address_key", return_value=""):
            self.assertEqual(recognition.params(config).final_model, "whisper")
            self.assertEqual(
                recognition.params(models.dataclasses.replace(config, provider="openrouter")).model, "облачная"
            )
        file = self.models.parent / "настройки.json"
        file.write_text(
            json.dumps({"провайдер": providers.LOCAL, "модель": "whisper-large-v3-turbo-int8"}), encoding="utf-8"
        )
        previous_items, _ = settings.read(file)  # old settings keep the local model in the shared model field
        self.assertEqual((previous_items.local_model, previous_items.model), ("whisper-large-v3-turbo-int8", ""))


if __name__ == "__main__":
    unittest.main()
