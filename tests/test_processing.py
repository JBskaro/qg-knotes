# SPDX-License-Identifier: GPL-3.0-only
"""Processing a whole meeting folder with a fake provider: response cache, chunks, notes, command line."""

import dataclasses
import json
import pathlib
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np

from knotes import catalog, meeting_jobs, spending
from knotes import settings
from knotes import providers
from knotes import recognition
from knotes import vocabulary
from tests.test_providers import curl_process

SR = recognition.SAMPLE_RATE


def _answer(*words: tuple[float, str, str], price: float = 0.01) -> dict:
    return {
        "words": [{"word": s, "start": n_, "end": n_ + 0.4, "speaker_label": m} for n_, s, m in words],
        "usage": {"cost": price},
    }


class TestProcessing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name) / "встреча"
        self.folder.mkdir()
        noise = np.random.default_rng(0).uniform(-0.5, 0.5, 2 * SR).astype(np.float32)
        microphone, computer = np.zeros(6 * SR, np.float32), np.zeros(6 * SR, np.float32)
        microphone[: 2 * SR], computer[3 * SR : 5 * SR] = noise, noise
        recognition.write_flac(microphone, self.folder / "микрофон.flac")
        recognition.write_flac(computer, self.folder / "собеседники.flac")
        self.params = recognition.Params(providers.find("openrouter"), "ключ")

    def tearDown(self):
        self._tmp.cleanup()

    def _recognize(self, _provider: providers.Provider, _key: str, request: providers.Request) -> dict:
        self.requests.append(request)
        if len(self.requests) == 1:
            return _answer((0.5, "Привет", "speaker_0"))
        return _answer((3.5, "Здравствуйте", "speaker_0"))

    def test_tracks_recognized_separately_and_cached(self):
        self.requests = []
        with mock.patch.object(providers, "recognize", self._recognize):
            file = recognition.process(self.folder, self.params)
            recognition.process(self.folder, self.params)
        text = file.read_text(encoding="utf-8")
        self.assertEqual(len(self.requests), 2)
        self.assertIn("[00:00] Я: Привет", text)
        self.assertIn("[00:03] Собеседник 1: Здравствуйте", text)
        self.assertIn("стоимость 0.020 $", text)
        cache = json.loads((self.folder / recognition.ANSWERS).read_text(encoding="utf-8"))
        self.assertEqual(set(cache), {"микрофон", "собеседники"})

    def test_tracks_measured_once_and_not_held(self):
        """Loudness and length are taken once per track and the audio is let go: the engine process reads the file
        itself, and two 3-hour tracks are 1.4 GB."""
        self.requests = []
        measured = mock.Mock(side_effect=recognition._measure)
        with (
            mock.patch.object(providers, "recognize", self._recognize),
            mock.patch.object(recognition, "_measure", measured),
            mock.patch.object(recognition, "read_mono", side_effect=AssertionError("the whole track as float32")),
        ):
            text = recognition.process(self.folder, self.params).read_text(encoding="utf-8")
        self.assertEqual(measured.call_count, 2)
        self.assertIn("[00:00] Я: Привет", text)
        self.assertIn("[00:03] Собеседник 1: Здравствуйте", text)

    def test_broken_answer_not_cached_and_old_one_dropped(self):
        """A response of an unknown format is not saved: "Retry" asks again instead of reading it from the cache; a
        broken cache of an earlier version is asked again too."""
        bad = '{"words": [{"text": "Привет", "start": 0.5, "end": 0.9}]}'
        with (
            mock.patch.object(providers, "_send", return_value=(200, bad)),
            self.assertLogs(providers.log, "WARNING"),
            self.assertRaises(providers.ProviderError),
        ):
            recognition.process(self.folder, self.params)
        self.assertFalse((self.folder / recognition.ANSWERS).exists())
        cache = {"микрофон": [{"сдвиг": 0, "ответ": json.loads(bad)}], "собеседники": [{"сдвиг": 0, "ответ": {}}]}
        (self.folder / recognition.ANSWERS).write_text(json.dumps(cache), encoding="utf-8")
        self.requests = []
        with (
            mock.patch.object(providers, "recognize", self._recognize),
            self.assertLogs(recognition.log, "WARNING"),
        ):
            text = recognition.process(self.folder, self.params).read_text(encoding="utf-8")
        self.assertEqual(len(self.requests), 1)
        self.assertIn("[00:00] Я: Привет", text)

    def test_long_track_goes_on_from_the_break(self):
        """The money ran out on a chunk of a long track: after topping up, "Retry" sends only the chunks not yet
        recognized, the paid ones are not paid again."""
        calls, payments, progress = [], [], []

        def recognize(_provider: providers.Provider, _key: str, request: providers.Request) -> dict:
            calls.append(request)
            if len(calls) == 5:
                raise providers.ProviderError("нет денег")
            return _answer((0.5, f"кусок{len(calls)}", "speaker_0"), price=0.01)

        with (
            mock.patch.object(providers, "recognize", recognize),
            mock.patch.object(recognition, "WHOLE_UNTIL", 1),
            mock.patch.object(recognition, "CHUNK_S", 2),
            mock.patch.object(recognition, "OVERLAP", 1),
        ):
            with self.assertRaises(providers.ProviderError):
                recognition.process(self.folder, self.params, payment=lambda _m, how_many: payments.append(how_many))
            self.assertEqual((len(calls), len(payments)), (5, 4))
            calls.clear()
            recognition.process(
                self.folder,
                self.params,
                progress=lambda done_count, total: progress.append((done_count, total)),
                payment=lambda _m, how_many: payments.append(how_many),
            )
        self.assertEqual((len(calls), len(payments)), (2, 6))
        self.assertEqual(progress, [(1, 3), (2, 3), (3, 3)])  # the chunk paid before the break counts as done
        cache = json.loads((self.folder / recognition.ANSWERS).read_text(encoding="utf-8"))
        self.assertEqual([[ch["сдвиг"] for ch in parts] for parts in cache.values()], [[0, 2, 4], [0, 2, 4]])
        self.assertFalse((self.folder / recognition.PARTS).exists())

    def test_each_paid_chunk_logged_at_once(self):
        """Spending is logged after each response: if the second track fails, the first is already paid for."""
        self.requests = []
        failure = [_answer((0.5, "Привет", "speaker_0"), price=0.03), providers.ProviderError("не принял")]
        with mock.patch.object(providers, "recognize", side_effect=failure), self.assertRaises(providers.ProviderError):
            meeting_jobs.recognize_folder(self.folder, self.params, (False, False), lambda *_: None)
        recordings = self.folder.parent
        expenses_ = spending.list_(recordings, catalog.list_(recordings))
        self.assertEqual(
            [(tr.meeting, tr.kind, tr.model, tr.how_many) for tr in expenses_],
            [("встреча", catalog.SPEECH, self.params.final_model, 0.03)],
        )

    def test_recognition_progress(self):
        self.requests, progress = [], []
        with mock.patch.object(providers, "recognize", self._recognize):
            recognition.process(
                self.folder, self.params, progress=lambda done_count, total: progress.append((done_count, total))
            )
        self.assertEqual(progress, [(0, 2), (1, 2), (2, 2)])
        self.assertEqual((recognition.part_count(60), recognition.part_count(2 * 3600)), (1, 4))

    def test_audio_chunk_of_both_tracks(self):
        data = recognition.audio_chunk(self.folder, 1.0, 3.0)
        audio = np.frombuffer(data, np.int16)
        self.assertAlmostEqual(len(audio) / SR, 3.0, delta=0.05)
        self.assertGreater(np.abs(audio[: SR // 2]).max(), 1000)  # from 1 s the microphone is still heard
        self.assertGreater(np.abs(audio[int(2.2 * SR) :]).max(), 1000)  # from 3 s, the other participants
        self.assertEqual(recognition.audio_chunk(self.folder.parent, 0, 1), b"")

    def test_temporary_failure_is_retried(self):
        answers = [
            providers.NetworkError("Нет интернета"),
            providers.TemporaryError("перегрузка"),
            _answer((0.5, "Да", "speaker_0")),
        ]
        with (
            mock.patch.object(providers, "recognize", side_effect=answers) as request,
            mock.patch.object(recognition.time, "sleep") as pause,
        ):
            result = recognition._recognize(self.params, b"mp3")
        self.assertEqual(
            (request.call_count, [v.args[0] for v in pause.call_args_list]), (3, list(recognition.RETRY_AFTER_S))
        )
        self.assertIn("words", result)
        with (
            mock.patch.object(providers, "recognize", side_effect=providers.NetworkError("Нет интернета")),
            mock.patch.object(recognition.time, "sleep"),
            self.assertRaises(providers.NetworkError),
        ):
            recognition._recognize(self.params, b"mp3")
        with (
            mock.patch.object(providers, "recognize", side_effect=providers.ProviderError("не принял ключ")) as request,
            mock.patch.object(recognition.time, "sleep"),
            self.assertRaises(providers.ProviderError),
        ):
            recognition._recognize(self.params, b"mp3")
        self.assertEqual(request.call_count, 1)  # key rejected: retrying is pointless

    def test_cancel_during_pause_before_retry(self):
        """Closing the program during the pause before a retry ends it at once: the pause watches the cancel."""
        cancel = threading.Event()
        cancel.set()
        params = dataclasses.replace(self.params, cancel=cancel)
        with (
            mock.patch.object(providers, "recognize", side_effect=providers.NetworkError("Нет интернета")) as request,
            mock.patch.object(recognition.time, "sleep", side_effect=AssertionError("a pause blind to the cancel")),
            self.assertRaises(providers.Cancelled),
        ):
            recognition._recognize(params, b"mp3")
        self.assertEqual(request.call_count, 1)
        self.assertIs(request.call_args.args[2].cancel, cancel)

    def test_empty_folder(self):
        with self.assertRaises(providers.ProviderError):
            recognition.process(pathlib.Path(self._tmp.name), self.params)

    def test_long_track_is_cut_into_chunks(self):
        calls = []

        def recognize(_pr: providers.Provider, _to: str, request: providers.Request) -> dict:
            calls.append(len(request.mp3))
            if len(calls) == 1:
                raise providers.SizeError("велико")
            return _answer((0.5, f"кусок{len(calls)}", "speaker_0"))

        with (
            mock.patch.object(providers, "recognize", recognize),
            mock.patch.object(recognition, "CHUNK_S", 2),
            mock.patch.object(recognition, "OVERLAP", 1),
        ):
            parts = recognition.recognize_track(self.folder / "микрофон.flac", 6, self.params)
        self.assertEqual([ch["сдвиг"] for ch in parts], [0, 2, 4])

    def test_chunk_not_accepted_in_time_told_plainly(self):
        """A 30-minute chunk of a long track did not pass either: a plain text, not the provider's page."""
        refusal = providers.SizeError("<html>524</html>")
        with (
            mock.patch.object(providers, "recognize", side_effect=refusal),
            mock.patch.object(recognition, "WHOLE_UNTIL", 1),
            self.assertLogs(recognition.log, "WARNING"),
            self.assertRaises(providers.ProviderError) as error,
        ):
            recognition.recognize_track(self.folder / "микрофон.flac", 6, self.params)
        self.assertNotIsInstance(error.exception, providers.SizeError)
        self.assertNotIn("<html>", str(error.exception))
        self.assertIn("OpenRouter", str(error.exception))

    def test_notes(self):
        silence = np.zeros(SR, np.float32)
        audio = np.ones(SR, np.float32) * 0.3
        silence, audio = recognition.loudness(silence), recognition.loudness(audio)
        self.assertIn("Готовая запись", recognition._notes(["общая"], {"общая": audio})[0])
        self.assertIn("Микрофон не записан", recognition._notes(["собеседники"], {"собеседники": audio})[0])
        self.assertIn(
            "очной", recognition._notes(["микрофон", "собеседники"], {"микрофон": audio, "собеседники": silence})[0]
        )
        self.assertEqual(recognition._notes(["микрофон", "собеседники"], {"микрофон": audio, "собеседники": audio}), [])

    def test_loudness_of_a_track_of_any_length(self):
        """A real recording is rarely a whole number of 50 ms windows: the tail shorter than a window is left out."""
        window = SR // 20
        for length in (window * 3 + 17, window * recognition.LEVEL_BLOCK + 5, window * (recognition.LEVEL_BLOCK + 2)):
            for audio in (np.full(length, 0.3, np.float32), np.full(length, 9830, np.int16)):
                with self.subTest(length=length, dtype=audio.dtype):
                    levels, anchor = recognition.loudness(audio)
                    self.assertEqual(levels.size, length // window)
                    self.assertAlmostEqual(anchor, 0.3, delta=0.01)

    def test_measure_reads_the_track_in_pieces(self):
        """Length and loudness are counted from ffmpeg piece by piece, the same as from the whole track; pieces end
        in the middle of a window too."""
        path = self.folder / "проба.flac"
        audio = np.concatenate([np.full(SR, 0.05, np.float32), np.full(SR * 2 + 333, 0.4, np.float32)])
        recognition.write_flac(audio, path)
        whole_seconds = len(recognition._read_int16(path)) / SR
        whole_levels, whole_anchor = recognition.loudness(recognition._read_int16(path))
        with mock.patch.object(recognition, "_read_int16", side_effect=AssertionError("the whole track was read")):
            seconds, (levels, anchor) = recognition._measure(path)
            with mock.patch.object(recognition, "LEVEL_BLOCK", 7):  # many pieces: 7 windows of 50 ms each
                small = recognition._measure(path)
        for got_seconds, (got_levels, got_anchor) in ((seconds, (levels, anchor)), small):
            self.assertAlmostEqual(got_seconds, whole_seconds)
            np.testing.assert_allclose(got_levels, whole_levels, atol=1e-6)
            self.assertAlmostEqual(got_anchor, whole_anchor, places=5)
        with self.assertRaises(subprocess.CalledProcessError), self.assertLogs(recognition.log, "WARNING"):
            recognition._measure(self.folder / "нет такого.flac")

    def test_params_from_settings(self):
        with mock.patch.object(settings, "address_key", return_value="к"):
            params = recognition.params(
                settings.Settings(
                    provider="openrouter",
                    vocabulary=[{"правильно": "ШЕ", "варианты": ["ше"]}],
                    provider_retention=True,
                    model="openai/whisper-1",
                )
            )
        self.assertEqual(
            (params.key, params.vocabulary, params.retention, params.final_model),
            ("к", (vocabulary.Word("ШЕ", ("ше",)),), True, "openai/whisper-1"),
        )

    def test_command_line(self):
        config = settings.Settings(provider="openrouter", recordings_folder=self._tmp.name)
        failure = providers.ProviderError("нет связи")
        with (
            mock.patch.object(settings, "load", return_value=config),
            mock.patch.object(settings, "address_key", return_value="к"),
            mock.patch.object(providers, "recognize", side_effect=failure),
        ):
            self.assertEqual(recognition.main([str(self.folder)]), 1)
        self.assertEqual(recognition.main([]), 2)


class TestCurl(unittest.TestCase):
    def _curl(self, exit_code: int, output: bytes = b"") -> tuple[int, str]:
        with curl_process(exit_code, output, "ошибка".encode()) as startup:
            answer = providers._curl("секрет", ["-X"], "https://пример")
        self.assertNotIn("секрет", " ".join(startup.call_args.args[0]))
        return answer

    def test_no_console_window(self):
        with curl_process() as startup:
            providers._curl("к", [], "https://пример")
        self.assertEqual(startup.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        result = subprocess.CompletedProcess([], 0, b"{}\n200", b"")
        with mock.patch.object(recognition.subprocess, "run", return_value=result) as run:
            recognition._ffmpeg("-version")
        self.assertEqual(run.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_answer_and_code(self):
        self.assertEqual(self._curl(0, b'{"a": 1}\n200'), (200, '{"a": 1}'))

    def test_no_connection_and_timeout(self):
        with self.assertRaises(providers.SizeError):
            self._curl(28)
        with self.assertRaises(providers.ProviderError):
            self._curl(6)

    def test_openrouter_body_goes_as_file(self):
        with mock.patch.object(providers, "_curl", return_value=(200, "{}")) as curl:
            providers.recognize(providers.find("openrouter"), "к", providers.Request(b"mp3", "ru"))
        self.assertIn("Content-Type: application/json", curl.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
