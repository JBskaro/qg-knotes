# SPDX-License-Identifier: GPL-3.0-only
"""Speech recognition on this computer: joining tokens into words, chunk times, speakers by overlap, response shape,
progress, child process and cancel, one process for many jobs with the model loaded once. sherpa-onnx is replaced
with a fake; a live check on real models is at the end, it runs only if the QG_KNOTES_MODELS models folder is set
(how to build it is in the TestLive docstring)."""

import dataclasses
import importlib.util
import inspect
import io
import logging
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
import wave
from unittest import mock

import numpy as np

from knotes import local_engine
from knotes import models
from knotes import providers
from knotes import recognition

SR = local_engine.SAMPLE_RATE
local_engine.log.addHandler(logging.NullHandler())  # assertLogs checks the warnings, they stay out of the test output


def _tokens(*triples: tuple) -> list[tuple[str, float, float]]:
    return list(triples)


class FakeEngine:
    """Engine without sherpa-onnx: chunks (start, length), per chunk the text and tokens from the chunk start,
    diarization segments."""

    def __init__(self, chunks: list[tuple[float, float]], tokens: list[list[tuple]], segments: list[tuple] = ()):
        self.chunks, self.tokens, self.segments = chunks, tokens, list(segments)
        self.recognized_count, self.diarized = 0, False

    def cut(self, audio: np.ndarray):
        for start, length in self.chunks:
            yield start, audio[int(start * SR) : int((start + length) * SR)]

    def recognize(self, chunk: np.ndarray):
        tokens = self.tokens[self.recognized_count % len(self.tokens)]  # a loaded engine serves job after job
        self.recognized_count += 1
        return " " + "".join(tr for tr, _, _ in tokens) + " ", tokens

    def diarize(self, audio: np.ndarray, progress):
        self.diarized = True
        progress(1, 2)
        progress(2, 2)
        return self.segments


class TestWords(unittest.TestCase):
    def test_gigaam_separate_space_and_punctuation(self):
        # how sherpa-onnx 2.0.0 returns GigaAM v3 tokens: "▁" is already replaced with a space, one letter each
        tokens = _tokens(
            (" ", 0.0, 0.04),
            ("Н", 0.04, 0.1),
            ("и", 0.1, 0.2),
            (",", 0.2, 0.3),
            (" не", 0.5, 0.7),
            (" ", 0.7, 0.8),
            ("т", 0.8, 0.9),
            ("ре", 0.9, 1.0),
        )
        words = local_engine.words_from_tokens(tokens, shift=10)
        self.assertEqual([s["word"] for s in words], ["Ни,", "не", "тре"])
        self.assertEqual([(s["start"], s["end"]) for s in words], [(10.04, 10.3), (10.5, 10.7), (10.8, 11.0)])

    def test_sentencepiece_word_start_sign(self):
        words = local_engine.words_from_tokens(_tokens(("▁При", 0, 1), ("вет", 1, 2), ("▁мир", 2, 3), (".", 3, 4)))
        self.assertEqual([s["word"] for s in words], ["Привет", "мир."])

    def test_whisper_space_at_token_start(self):
        words = local_engine.words_from_tokens(_tokens((" God", 0, 1), (",", 1, 2), (" as", 2, 3), (" a", 3, 4)))
        self.assertEqual([s["word"] for s in words], ["God,", "as", "a"])

    def test_chunk_always_starts_with_new_word(self):
        words = local_engine.words_from_tokens(_tokens(("при", 0, 1), ("вет", 1, 2)))
        self.assertEqual([s["word"] for s in words], ["привет"])
        self.assertEqual(local_engine.words_from_tokens([]), [])


def _result(**fields) -> types.SimpleNamespace:
    empty = {
        "text": "",
        "tokens": [],
        "timestamps": [],
        "durations": [],
        "segment_timestamps": [],
        "segment_durations": [],
        "segment_texts": [],
    }
    return types.SimpleNamespace(**empty | fields)


class TestTime(unittest.TestCase):
    def test_token_end_not_past_next_and_chunk_end(self):
        result = _result(tokens=["а", "б", "в"], timestamps=[0.0, 0.1, 1.0])
        self.assertEqual(local_engine.token_times(result, 1.1), [(0.0, 0.1), (0.1, 0.35), (1.0, 1.1)])

    def test_token_durations_when_model_gives_them(self):
        result = _result(tokens=["а", "б"], timestamps=[0.0, 0.5], durations=[0.2, 0.3])
        self.assertEqual(local_engine.token_times(result, 1.0), [(0.0, 0.2), (0.5, 0.8)])

    def test_whisper_without_token_times_splits_by_segments(self):
        # Whisper from the sherpa-onnx releases: no token times, only segment times
        result = _result(
            tokens=[" раз", " два", " три", " ч", "еты", "ре"],
            segment_timestamps=[0.0, 5.0],
            segment_durations=[2.0, 3.0],
            segment_texts=[" раз два", " три четыре"],
        )
        times = local_engine.token_times(result, 10.0)
        self.assertEqual(times[0], (0.0, 1.0))
        self.assertEqual(times[1], (1.0, 2.0))
        self.assertEqual(times[2][0], 5.0)
        self.assertAlmostEqual(times[-1][1], 8.0)
        self.assertEqual(sorted(times), times)

    def test_without_any_times_tokens_share_whole_chunk(self):
        result = _result(tokens=[" да", " нет"])
        self.assertEqual(local_engine.token_times(result, 7.0), [(0.0, 3.0), (3.0, 7.0)])  # by share of letters
        self.assertEqual(local_engine.token_times(_result(), 2.0), [])


class TestSpeakers(unittest.TestCase):
    def _words(self, *pairs: tuple) -> list[dict]:
        return [{"start": n, "end": k, "word": "с"} for n, k in pairs]

    def test_most_overlap(self):
        segments = [(0.0, 1.2, 5), (1.0, 3.0, 2)]
        self.assertEqual(
            local_engine.speakers(self._words((0.9, 1.5), (0.1, 0.5)), segments), ["speaker_1", "speaker_0"]
        )

    def test_word_outside_segments_gets_nearest(self):
        segments = [(0.0, 1.0, 3), (5.0, 6.0, 7)]
        self.assertEqual(
            local_engine.speakers(self._words((1.5, 2.0), (4.0, 4.5), (9, 10)), segments),
            ["speaker_0", "speaker_1", "speaker_1"],
        )

    def test_numbers_in_order_of_appearance(self):
        segments = [(4.0, 5.0, 0), (0.0, 1.0, 8), (2.0, 3.0, 3)]
        self.assertEqual(
            local_engine.speakers(self._words((0.2, 0.4), (2.2, 2.4), (4.2, 4.4)), segments),
            ["speaker_0", "speaker_1", "speaker_2"],
        )

    def test_without_diarization_one_speaker(self):
        self.assertEqual(local_engine.speakers(self._words((0, 1), (2, 3)), []), ["speaker_0", "speaker_0"])

    def test_speech_fragment_is_not_a_separate_speaker(self):
        """A speaker with less than 2% of the speech is a fragment of another voice: their words go to the neighboring
        speaker."""
        segments = [(0.0, 50.0, 3), (50.0, 50.5, 8), (50.5, 100.0, 4)]
        self.assertEqual(
            local_engine.speakers(self._words((10, 11), (50.1, 50.4), (60, 61)), segments),
            ["speaker_0", "speaker_0", "speaker_1"],
        )


MODEL = local_engine.Model(
    {"model": "модель/model.onnx", "tokens": "модель/tokens.txt"},
    "nemo_ctc",
    "vad/silero_vad.onnx",
    "GigaAM v3",
    id_="gigaam-v3",
)
DIARIZATION = local_engine.Diarization("сегменты/model.onnx", "голос/voice.onnx")


def _engine() -> FakeEngine:
    return FakeEngine(
        [(1.0, 2.0), (6.0, 2.0)],
        [
            _tokens((" Добрый", 0.1, 0.5), (" день", 0.6, 0.9), (",", 0.9, 1.0)),
            _tokens((" Как", 0.0, 0.3), (" дела", 0.4, 0.8), ("?", 0.8, 0.9)),
        ],
        [(0.9, 3.0, 4), (5.5, 8.0, 1)],
    )


class TestAnswer(unittest.TestCase):
    def test_answer_like_cloud_and_understood_by_providers(self):
        answer = local_engine.recognize(np.zeros(10 * SR, np.float32), MODEL, DIARIZATION, engine=_engine())
        self.assertEqual(answer["text"], "Добрый день, Как дела?")
        self.assertEqual(answer["duration"], 10.0)
        self.assertEqual(answer["words"][0], {"start": 1.1, "end": 1.5, "word": "Добрый", "speaker": "speaker_0"})
        words = providers.words(answer)
        self.assertEqual(
            [(s["начало"], s["слово"], s["говорящий"]) for s in words],
            [
                (1.1, "Добрый", "speaker_0"),
                (1.6, "день,", "speaker_0"),
                (6.0, "Как", "speaker_1"),
                (6.4, "дела?", "speaker_1"),
            ],
        )
        phrases = recognition.phrases(
            recognition.words_from([{"сдвиг": 0, "ответ": answer}])
        )  # from here on, as with a cloud response
        self.assertEqual([f["текст"] for f in phrases], ["Добрый день,", "Как дела?"])

    def test_without_diarization_one_speaker_and_no_diarization_run(self):
        engine = _engine()
        answer = local_engine.recognize(np.zeros(10 * SR, np.float32), MODEL, None, engine=engine)
        self.assertFalse(engine.diarized)
        self.assertEqual({s["speaker"] for s in answer["words"]}, {"speaker_0"})

    def test_progress_in_audio_seconds(self):
        progress: list[tuple[int, int]] = []
        local_engine.recognize(np.zeros(10 * SR, np.float32), MODEL, None, lambda *x_: progress.append(x_), _engine())
        self.assertEqual(progress, [(0, 10), (3, 10), (8, 10), (10, 10)])
        progress.clear()
        local_engine.recognize(
            np.zeros(10 * SR, np.float32), MODEL, DIARIZATION, lambda *x_: progress.append(x_), _engine()
        )
        self.assertEqual(progress, [(0, 10), (2, 10), (4, 10), (8, 10), (10, 10)])  # the second half is diarization

    def test_silence(self):
        answer = local_engine.recognize(np.zeros(0, np.float32), MODEL, None, engine=FakeEngine([], []))
        self.assertEqual(answer, {"text": "", "duration": 0.0, "words": []})


class FakeVad:
    """Collects audio and returns the given chunks after flush, like VoiceActivityDetector. As in the real one, front
    is a reference inside: after pop() it holds garbage."""

    def __init__(self, so: "FakeSherpa"):
        self.so, self.accepted_count, self.ready_items = so, [], []

    def accept_waveform(self, audio):
        self.so.portions.append(len(audio))
        self.accepted_count.extend(audio)

    def flush(self):
        self.ready_items = [
            types.SimpleNamespace(start=n, samples=self.accepted_count[n : n + d]) for n, d in self.so.chunks
        ]

    def empty(self):
        return not self.ready_items

    @property
    def front(self):
        return self.ready_items[0]

    def pop(self):
        segment = self.ready_items.pop(0)
        segment.start, segment.samples = -1, [7e30] * len(segment.samples)


class FakeSherpa(types.SimpleNamespace):
    """Fake sherpa_onnx module: remembers what was opened and with which params, returns the given speech chunks
    (start and length in samples), the same recognition result every time and the diarization segments."""

    def __init__(self, chunks: list[tuple[int, int]], failure: Exception | None = None):
        super().__init__(open_count={}, portions=[], chunks=chunks, failure=failure)
        config_text = types.SimpleNamespace
        self.VadModelConfig = config_text
        self.SileroVadModelConfig = lambda **k: config_text(window_size=512, **k)
        self.OfflineSpeakerDiarizationConfig = self.OfflineSpeakerSegmentationModelConfig = config_text
        self.OfflineSpeakerSegmentationPyannoteModelConfig = self.SpeakerEmbeddingExtractorConfig = config_text
        self.FastClusteringConfig = config_text
        self.OfflineRecognizer = config_text(**{v.method: self._recognizer(v.method) for v in models.KINDS.values()})

    def _recognizer(self, method: str):
        def open_view(**params):
            if self.failure:
                raise self.failure
            self.open_count[method] = params
            stream = types.SimpleNamespace(accept_waveform=lambda frequency, audio: None, result=None)
            result = _result(text=" да", tokens=[" да"], timestamps=[0.0])
            return types.SimpleNamespace(
                create_stream=lambda: stream, decode_stream=lambda p: setattr(p, "result", result)
            )

        return open_view

    def VoiceActivityDetector(self, config):  # noqa: N802 - name as in sherpa_onnx
        return FakeVad(self)

    def OfflineSpeakerDiarization(self, config):  # noqa: N802
        self.open_count["разметка"] = config

        def process(audio, callback):
            callback(1, 1)
            segments = [types.SimpleNamespace(start=0.0, end=1.0, speaker=9)]
            return types.SimpleNamespace(sort_by_start_time=lambda: segments)

        return types.SimpleNamespace(process=process)


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.folder = pathlib.Path(tempfile.mkdtemp())
        for path in (
            "giga/model.onnx",
            "giga/model.int8.onnx",
            "giga/tokens.txt",
            "whisper/turbo-encoder.int8.onnx",
            "whisper/turbo-decoder.int8.onnx",
            "whisper/turbo-tokens.txt",
            "vad/silero_vad.onnx",
            "сегменты/model.int8.onnx",
            "голос/campplus.onnx",
        ):
            (self.folder / path).parent.mkdir(parents=True, exist_ok=True)
            (self.folder / path).write_bytes(b"onnx")
        self.model = local_engine.Model(
            self._files("giga", "model.int8.onnx", "tokens.txt"),
            "nemo_ctc",
            self._path("vad/silero_vad.onnx"),
            "GigaAM v3",
        )
        self.diarization = local_engine.Diarization(
            self._path("сегменты/model.int8.onnx"), self._path("голос/campplus.onnx")
        )

    def _path(self, path: str) -> str:
        return str(self.folder / path)

    def _files(self, folder: str, *names: str) -> dict[str, str]:
        return {role: self._path(f"{folder}/{name}") for role, name in models.roles(names).items()}

    def test_model_files_and_params(self):
        """The app gives the file paths: the engine does not scan the folder."""
        so = FakeSherpa([])
        local_engine.Engine(self.model, None, so)
        self.assertEqual(so.open_count["from_nemo_ctc"]["model"], self._path("giga/model.int8.onnx"))
        self.assertGreaterEqual(so.open_count["from_nemo_ctc"]["num_threads"], 1)
        files = self._files("whisper", "turbo-encoder.int8.onnx", "turbo-decoder.int8.onnx", "turbo-tokens.txt")
        whisper = local_engine.Model(files, "whisper", self._path("vad/silero_vad.onnx"), "Whisper", "ru")
        local_engine.Engine(whisper, self.diarization, so)
        params = so.open_count["from_whisper"]
        self.assertEqual(pathlib.Path(params["encoder"]).name, "turbo-encoder.int8.onnx")
        self.assertEqual(params["language"], "ru")
        self.assertTrue(params["enable_segment_timestamps"])
        self.assertEqual(so.open_count["разметка"].clustering.num_clusters, -1)  # speaker count is auto-detected

    def test_gigaam_uses_directml_and_falls_back_to_cpu(self):
        model = dataclasses.replace(self.model, id_="gigaam-v3-ctc-punct-int8")
        self.assertTrue(local_engine._use_directml(model))
        so = FakeSherpa([])
        local_engine.Engine(model, None, so)
        self.assertEqual(so.open_count["from_nemo_ctc"]["provider"], "directml")
        so = FakeSherpa([])
        original = so.OfflineRecognizer.from_nemo_ctc
        providers_used = []

        def open_view(**params):
            providers_used.append(params["provider"])
            if params["provider"] == "directml":
                raise RuntimeError("DirectML is unavailable")
            return original(**params)

        so.OfflineRecognizer.from_nemo_ctc = open_view
        with self.assertLogs(local_engine.log, "WARNING"):
            local_engine.Engine(model, None, so)
        self.assertEqual(providers_used, ["directml", "cpu"])
        self.assertEqual(so.open_count["from_nemo_ctc"]["provider"], "cpu")
        self.assertFalse(local_engine._use_directml(dataclasses.replace(model, id_="other-model")))

    def test_all_model_kinds(self):
        """Each kind is opened by its own sherpa-onnx constructor: files by role, its own params, the meeting language
        only for those that ask for it."""
        names = {
            "encoder": "encoder.int8.onnx",
            "decoder": "decoder.onnx",
            "joiner": "joiner.int8.onnx",
            "tokens": "tokens.txt",
            "model": "model.int8.onnx",
        }
        for key_, kind in models.KINDS.items():
            with self.subTest(kind=key_):
                folder = self.folder / "виды" / key_
                folder.mkdir(parents=True)
                for role in kind.files.values():
                    (folder / names[role]).write_bytes(b"onnx")
                so = FakeSherpa([])
                files = {r: str(folder / names[r]) for r in kind.files.values()}
                local_engine.Engine(
                    local_engine.Model(files, key_, self._path("vad/silero_vad.onnx"), key_, "ru"), None, so
                )
                params = so.open_count[kind.method]
                self.assertEqual(
                    {p_: pathlib.Path(params[p_]).name for p_ in kind.files},
                    {p_: names[r] for p_, r in kind.files.items()},
                )
                self.assertLessEqual(kind.other.items(), params.items())
                self.assertEqual(params.get("language"), "ru" if kind.with_language else None)

    def test_vad_windows_with_margin_and_long_speech_cut(self):
        so = FakeSherpa([(SR, 2 * SR), (4 * SR, 50 * SR)])
        chunks = list(local_engine.Engine(self.model, None, so).cut(np.zeros(60 * SR, np.float32)))
        self.assertEqual(max(so.portions), 512)  # with large blocks VAD does not cut speech at the length limit
        # a 0.25 s margin on both sides, but not into the previous chunk
        self.assertEqual(
            [(round(start, 3), round(len(chunk) / SR, 3)) for start, chunk in chunks],
            [(0.75, 2.5), (3.75, 16.833), (20.583, 16.833), (37.417, 16.833)],
        )

    def test_recognition_and_diarization_via_fake_sherpa(self):
        so = FakeSherpa([(SR, 2 * SR)])
        progress: list[tuple] = []
        answer = local_engine.recognize(
            np.zeros(4 * SR, np.float32),
            self.model,
            self.diarization,
            lambda *x_: progress.append(x_),
            local_engine.Engine(self.model, self.diarization, so),
        )
        self.assertEqual(answer["words"], [{"start": 0.75, "end": 1.0, "word": "да", "speaker": "speaker_0"}])
        self.assertEqual(progress[-1], (4, 4))

    def test_model_not_downloaded(self):
        none_ = self._path("нет/model.onnx")
        no_file = dataclasses.replace(self.model, files={"model": none_, "tokens": self._path("giga/tokens.txt")})
        no_role = dataclasses.replace(self.model, files={"model": self._path("giga/model.int8.onnx")})
        for model, diarization, title in (
            (no_file, None, "GigaAM v3"),
            (no_role, None, "GigaAM v3"),
            (dataclasses.replace(self.model, vad=none_), None, "GigaAM v3"),
            (self.model, local_engine.Diarization(none_, self.diarization.voice), "Разметка говорящих"),
        ):
            with self.subTest(title=title), self.assertRaises(providers.ProviderError) as error:
                local_engine.Engine(model, diarization, FakeSherpa([]))
            self.assertIn(f"Модель «{title}» не скачана", str(error.exception))

    def test_broken_model_and_out_of_memory(self):
        custom = dataclasses.replace(self.model, custom=True)
        for model, failure, text in (
            (self.model, RuntimeError("Load model failed:Protobuf parsing failed."), "«GigaAM v3» повреждена"),
            (custom, RuntimeError("Load model failed"), "«GigaAM v3» не открылась: программа ее не понимает"),
            (self.model, RuntimeError("bad allocation"), "Не хватает памяти"),
            (self.model, MemoryError(), "Не хватает памяти"),
        ):
            with (
                self.subTest(failure=failure, custom=model.custom),
                self.assertLogs(local_engine.log, "WARNING"),
                self.assertRaises(providers.ProviderError) as error,
            ):
                local_engine.Engine(model, None, FakeSherpa([], failure))
            self.assertIn(text, str(error.exception))


class TestChild(unittest.TestCase):
    def _task_input(self) -> dict:
        return local_engine._task_input(pathlib.Path("встреча/собеседники.flac"), MODEL, DIARIZATION)

    def test_job_round_trip(self):
        job = json.loads(json.dumps(self._task_input()))
        self.assertEqual(
            local_engine._from_job(job), (str(pathlib.Path("встреча/собеседники.flac")), MODEL, DIARIZATION)
        )
        self.assertIsNone(local_engine._from_job(job | {"разметка": None})[2])

    def _launch(self, arguments: list[str], stdin: str = "", engine: object = None) -> tuple[int, list[str]]:
        output = io.StringIO()
        with (
            mock.patch.object(local_engine, "_read", return_value=np.zeros(10 * SR, np.float32)),
            mock.patch.object(local_engine, "Engine", return_value=engine or _engine()),
        ):
            code = local_engine.main(arguments, io.StringIO(stdin), output)
        return code, output.getvalue().splitlines()

    def test_job_via_stdin_progress_and_answer_as_json_lines(self):
        code, lines = self._launch(["программа", local_engine.FLAG], json.dumps(self._task_input()))
        self.assertEqual(code, 0)
        self.assertTrue(all(line.isascii() for line in lines))
        progress: list[tuple] = []
        answer = local_engine.parse(lines, lambda *x_: progress.append(x_))
        self.assertEqual(answer["text"], "Добрый день, Как дела?")
        self.assertEqual(progress[0], (0, 10))
        self.assertEqual(progress[-1], (10, 10))

    def test_user_errors_as_one_line(self):
        for failure, text in (
            (providers.ProviderError("Модель не скачана"), "Модель не скачана"),
            (RuntimeError("Failed to allocate memory"), "Не хватает памяти"),
            (MemoryError(), "Не хватает памяти"),
        ):
            engine = mock.Mock(cut=mock.Mock(side_effect=failure))
            with self.subTest(failure=failure), mock.patch.object(local_engine.log, "warning"):
                code, lines = self._launch([local_engine.FLAG], json.dumps(self._task_input()), engine)
            self.assertEqual(code, 1)
            with self.assertRaises(providers.ProviderError) as error:
                local_engine.parse(lines)
            self.assertIn(text, str(error.exception))

    def test_other_engine_failure_logged_and_plain(self):
        engine = mock.Mock(cut=mock.Mock(side_effect=ValueError("что-то внутри")))
        with self.assertLogs(local_engine.log, "ERROR") as log:
            code, lines = self._launch([local_engine.FLAG], json.dumps(self._task_input()), engine)
        self.assertEqual(code, 1)
        self.assertIn("что-то внутри", "\n".join(log.output))
        self.assertIn("прервалось", json.loads(lines[-1])["ошибка"])

    def test_parsing_skips_foreign_lines(self):
        with self.assertLogs(local_engine.log, "WARNING"):
            self.assertEqual(
                local_engine.parse(
                    [
                        b"warning from onnxruntime\n",
                        b'{"answer": 1}\n',
                        b"[1, 2]\n",
                        b'"x"\n',
                        b'{"\\u043e\\u0442\\u0432\\u0435\\u0442": {"text": ""}}\n',
                    ]
                ),
                {"text": ""},
            )
        self.assertIsNone(local_engine.parse([json.dumps({"ход": [1, 2]})]))

    def test_audio_read_like_for_cloud(self):
        with mock.patch.object(recognition, "read_mono", return_value=np.ones(3, np.float32)) as read:
            self.assertEqual(len(local_engine._read("собеседники.flac")), 3)
        read.assert_called_once_with(pathlib.Path("собеседники.flac"))

    def test_error_language_like_program(self):
        try:
            with mock.patch.object(local_engine.log, "warning"):
                _, lines = self._launch(
                    [local_engine.FLAG],
                    json.dumps(self._task_input() | {"язык": "en"}),
                    mock.Mock(cut=mock.Mock(side_effect=MemoryError())),
                )
        finally:
            local_engine.language.set_("ru")
        self.assertIn("Not enough memory", json.loads(lines[-1])["ошибка"])

    def _jobs(self, *jobs: dict) -> io.StringIO:
        return io.StringIO("".join(json.dumps(job) + "\n" for job in jobs))

    def _serve(self, stdin: io.StringIO, load: mock.Mock) -> tuple[int, list[str]]:
        output = io.StringIO()
        with (
            mock.patch.object(local_engine, "_read", return_value=np.zeros(10 * SR, np.float32)),
            mock.patch.object(local_engine, "Engine", load),
        ):
            code = local_engine.main([local_engine.FLAG], stdin, output)
        return code, output.getvalue().splitlines()

    def test_jobs_line_after_line_model_loads_once(self):
        """The transcript during the meeting sends piece after piece to one process: the model loads with the first job
        and stays loaded; another model replaces it; the stop line ends the process, the lines after it are not read."""
        job = self._task_input()
        whisper = job | {"модель": dataclasses.asdict(dataclasses.replace(MODEL, kind="whisper", title="Whisper"))}
        load = mock.Mock(side_effect=lambda *_: _engine())
        code, lines = self._serve(self._jobs(job, job, job, whisper, {"стоп": True}, job), load)
        self.assertEqual(code, 0)
        self.assertEqual([c.args[0].title for c in load.call_args_list], ["GigaAM v3", "Whisper"])
        answers = iter(lines)
        texts = [local_engine.parse(answers)["text"] for _ in range(4)]
        self.assertEqual(texts, ["Добрый день, Как дела?"] * 4)
        self.assertIsNone(local_engine.parse(answers))  # nothing after the stop line

    def test_failed_job_does_not_end_the_engine(self):
        """A model that did not load is loaded again by the next job; the input closed (the program exited or died)
        ends the process with the code of the last job."""
        job = self._task_input()
        load = mock.Mock(side_effect=[providers.ProviderError("Модель не скачана"), _engine()])
        code, lines = self._serve(self._jobs(job, job), load)
        self.assertEqual(load.call_count, 2)
        answers = iter(lines)
        with self.assertRaises(providers.ProviderError):
            local_engine.parse(answers)
        self.assertEqual(local_engine.parse(answers)["text"], "Добрый день, Как дела?")
        self.assertEqual(code, 0)
        code, _ = self._serve(self._jobs(job), mock.Mock(side_effect=providers.ProviderError("Модель не скачана")))
        self.assertEqual(code, 1)

    def test_engine_exits_when_program_is_gone(self):
        """Nobody reads the answers any more: the engine exits at once instead of recognizing for nobody."""

        class Gone(io.StringIO):
            def write(self, _text):
                raise BrokenPipeError(32, "The pipe is being closed")

        with (
            mock.patch.object(local_engine, "_read", return_value=np.zeros(10 * SR, np.float32)),
            mock.patch.object(local_engine, "Engine", return_value=_engine()),
            mock.patch.object(local_engine.os, "_exit", side_effect=SystemExit) as end,
            self.assertRaises(SystemExit),
        ):
            local_engine.main([local_engine.FLAG], self._jobs(self._task_input()), Gone())
        end.assert_called_once_with(0)


# Child process for the test: behaves like main() by the command in the job's "звук" field
CHILD = textwrap.dedent("""
    import json, sys, time
    job = json.load(sys.stdin)
    command = job["звук"]
    print(json.dumps({"ход": [0, 2]}), flush=True)
    if command == "ответ":
        print(json.dumps({"ход": [2, 2]}), flush=True)
        print(json.dumps({"ответ": {"text": job["модель"]["title"], "words": []}}), flush=True)
    elif command == "ошибка":
        print(json.dumps({"ошибка": "Модель не скачана"}), flush=True)
    elif command == "сбой":
        print("Traceback: engine crashed", file=sys.stderr)
        sys.exit(3)
    elif command == "авария":
        sys.exit(-1073741819)  # 0xC0000005
    elif command == "не справилась":
        sys.exit(1)
    else:
        time.sleep(60)
""")


class TestProcess(unittest.TestCase):
    def _recognize(self, command: str, progress=None, cancel=None) -> dict:
        with mock.patch.object(local_engine.system, "startup", return_value=([sys.executable, "-c", CHILD], {})):
            return local_engine.recognize_in_process(pathlib.Path(command), MODEL, None, progress, cancel)

    def test_progress_and_answer(self):
        progress: list[tuple] = []
        self.assertEqual(self._recognize("ответ", lambda *x_: progress.append(x_)), {"text": "GigaAM v3", "words": []})
        self.assertEqual(progress, [(0, 2), (2, 2)])

    def test_engine_error_as_text_for_user(self):
        with self.assertRaises(providers.ProviderError) as error:
            self._recognize("ошибка")
        self.assertEqual(str(error.exception), "Модель не скачана")

    def test_process_crash_with_code_and_messages_logged(self):
        with self.assertRaises(providers.ProviderError) as error, self.assertLogs(local_engine.log, "WARNING") as log:
            self._recognize("сбой")
        self.assertIn("(код 3)", str(error.exception))
        self.assertIn("engine crashed", "\n".join(log.output))
        self.assertIn("model gigaam-v3 (nemo_ctc), audio 2 s, diarization no", "\n".join(log.output))

    def test_crash_codes_in_plain_text(self):
        for command, text, code in (
            ("авария", "аварийно завершился на модели «GigaAM v3»", "0xC0000005"),
            ("не справилась", "«GigaAM v3» не справилась", "0x1 "),
        ):
            with (
                self.subTest(command=command),
                self.assertRaises(providers.ProviderError) as error,
                self.assertLogs(local_engine.log, "WARNING") as log,
            ):
                self._recognize(command)
            self.assertIn(text, str(error.exception))
            self.assertIn(f"code {code}", "\n".join(log.output))
        self.assertIn("памяти", local_engine._crash(0xC0000017, MODEL))

    def test_silence_means_hung(self):
        with (
            mock.patch.object(local_engine, "SILENCE_S", 0.5),
            self.assertRaises(providers.ProviderError) as error,
            self.assertLogs(local_engine.log, "WARNING"),
        ):
            self._recognize("ждать")
        self.assertIn("завис", str(error.exception))

    def test_native_output_does_not_mix_with_answer(self):
        """Exchange with the app goes through a copy of stdout: printing to stdout goes to stderr."""
        checking = (
            "from knotes import local_engine; channel = local_engine._channel(); print('шум', flush=True); "
            "channel.write('{}\\n'); channel.flush()"
        )
        result = subprocess.run(
            [sys.executable, "-c", checking],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=os.environ | {"PYTHONPATH": os.pathsep.join(sys.path)},
            check=True,
        )
        self.assertEqual((result.stdout, result.stderr.strip()), ("{}\n", "шум"))

    def test_cancel_ends_process(self):
        cancel = threading.Event()
        start = time.monotonic()
        with self.assertRaises(local_engine.Cancelled):
            self._recognize("ждать", lambda *_: cancel.set(), cancel)
        self.assertLess(time.monotonic() - start, 30)

    def test_no_window_and_lower_priority(self):
        with (
            mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
            mock.patch.object(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000, create=True),
        ):
            self.assertEqual(local_engine._flags(), 0x08004000)

    def test_engine_process_does_not_load_windows_settings_and_sherpa(self):
        checking = (
            "import sys, knotes.local_engine; print(sorted(m for m in sys.modules if m.startswith("
            "('PySide6', 'sherpa_onnx', 'knotes.settings', 'knotes.ui', 'knotes.recognition'))))"
        )
        result = subprocess.run(
            [sys.executable, "-c", checking],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=os.environ | {"PYTHONPATH": os.pathsep.join(sys.path)},
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "[]")


# Engine process of many jobs for the test: takes job line after job line and behaves by the command in "звук"; the
# answer tells the process and the job number in it
SERVER = textwrap.dedent("""
    import json, os, sys, time
    count, deaf = 0, False
    for line in sys.stdin:
        job = json.loads(line)
        if job.get("стоп"):
            time.sleep(60 if deaf else 0)
            sys.exit(0)
        command, count = job["звук"], count + 1
        print(json.dumps({"ход": [count, count]}), flush=True)
        if command == "медленно":
            time.sleep(0.8)
        if command in ("ответ", "глухой", "медленно"):
            deaf = deaf or command == "глухой"  # does not hear the stop line
            print(json.dumps({"ответ": {"text": job["модель"]["title"], "pid": os.getpid(), "job": count}}), flush=True)
        elif command == "ошибка":
            print(json.dumps({"ошибка": "Модель не скачана"}), flush=True)
        elif command == "сбой":
            print("Traceback: engine crashed", file=sys.stderr, flush=True)
            sys.exit(3)
        else:
            time.sleep(60)
    sys.exit(5)  # the input closed without the stop line
""")


class TestServer(unittest.TestCase):
    """One engine process for many jobs (the transcript during the meeting): started with the first job, it answers job
    after job; a crash or a hang is a plain text, the next job starts a new process, but not at once."""

    def setUp(self):
        self.starts: list[float] = []
        popen = subprocess.Popen

        def start(*arguments, **named):
            self.starts.append(time.monotonic())
            self.assertEqual(named["creationflags"], local_engine._flags())  # lower priority as after the meeting
            return popen(*arguments, **named)

        for patcher in (
            mock.patch.object(local_engine.system, "startup", return_value=([sys.executable, "-c", SERVER], {})),
            mock.patch.object(local_engine.subprocess, "Popen", side_effect=start),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = local_engine.Server()
        self.addCleanup(self.server.close)

    def _recognize(self, command: str, progress=None, cancel=None) -> dict:
        return self.server.recognize(pathlib.Path(command), MODEL, None, progress, cancel)

    def test_job_after_job_in_one_process_stop_line_ends_it(self):
        progress: list[tuple] = []
        answers = [self._recognize("ответ", lambda *x_: progress.append(x_)) for _ in range(3)]
        self.assertEqual(len(self.starts), 1)
        self.assertEqual([a["job"] for a in answers], [1, 2, 3])
        self.assertEqual(len({a["pid"] for a in answers}), 1)
        self.assertEqual(answers[0]["text"], "GigaAM v3")
        self.assertEqual(progress, [(1, 1), (2, 2), (3, 3)])
        process_ = self.server._process  # noqa: SLF001
        self.server.close()
        self.assertEqual(process_.returncode, 0)  # idle: it exited by the stop line, it was not killed
        self.server.close()  # a second time: nothing to end

    def test_engine_error_keeps_the_process(self):
        first = self._recognize("ответ")["pid"]
        with self.assertRaises(providers.ProviderError) as error:
            self._recognize("ошибка")
        self.assertEqual(str(error.exception), "Модель не скачана")
        self.assertEqual(self._recognize("ответ")["pid"], first)

    def test_crash_in_plain_text_and_new_process_not_at_once(self):
        with mock.patch.object(local_engine, "RESTART_S", 0.5):
            first = self._recognize("ответ")["pid"]
            with (
                self.assertLogs(local_engine.log, "WARNING") as log,
                self.assertRaises(providers.ProviderError) as error,
            ):
                self._recognize("сбой")
            answer = self._recognize("ответ")
        self.assertEqual(str(error.exception), local_engine.tr(local_engine.ENGINE_FELL, title="GigaAM v3", code="0x3"))
        self.assertIn("engine crashed", "\n".join(log.output))
        self.assertIn("model gigaam-v3 (nemo_ctc), audio 2 s, diarization no", "\n".join(log.output))
        self.assertNotEqual(answer["pid"], first)
        self.assertEqual(answer["job"], 1)
        self.assertEqual(len(self.starts), 2)
        self.assertGreaterEqual(self.starts[1] - self.starts[0], 0.5)
        self.assertIn("памяти", local_engine._fell(0xC0000017, MODEL))

    def test_hung_job_ends_the_process(self):
        """A job without a line longer than JOB_SILENCE_S hung; the first job of a process also loads the model and
        waits as long as after the meeting."""
        with mock.patch.object(local_engine, "JOB_SILENCE_S", 0.3), mock.patch.object(local_engine, "RESTART_S", 0):
            self.assertEqual(self._recognize("медленно")["job"], 1)
            with (
                self.assertLogs(local_engine.log, "WARNING") as log,
                self.assertRaises(providers.ProviderError) as error,
            ):
                self._recognize("медленно")
            self.assertIsNone(self.server._process)  # noqa: SLF001
            self.assertEqual(self._recognize("медленно")["job"], 1)  # a new process, its first job waits longer
        self.assertEqual(str(error.exception), local_engine.tr(local_engine.JOB_HUNG))
        self.assertIn("(hung)", "\n".join(log.output))

    def test_cancel_cuts_off_the_job_and_the_wait_for_a_restart(self):
        cancel = threading.Event()
        begin = time.monotonic()
        threading.Timer(0.5, cancel.set).start()
        with self.assertRaises(local_engine.Cancelled):
            self._recognize("ждать", cancel=cancel)
        with self.assertRaises(local_engine.Cancelled):
            self._recognize("ответ", cancel=cancel)  # cancelled already: no new process
        self.assertEqual(len(self.starts), 1)
        with (
            mock.patch.object(local_engine, "RESTART_S", 60),
            self.assertRaises(local_engine.Cancelled),
        ):
            cancel = threading.Event()
            threading.Timer(0.5, cancel.set).start()
            self._recognize("ответ", cancel=cancel)  # the killed process is started again not sooner than in a minute
        self.assertLess(time.monotonic() - begin, 30)
        self.assertEqual(len(self.starts), 1)

    def test_engine_gone_between_jobs_is_started_again(self):
        first = self._recognize("ответ")["pid"]
        self.server._process.kill()  # noqa: SLF001
        self.server._process.wait()  # noqa: SLF001
        with mock.patch.object(local_engine, "RESTART_S", 0), self.assertLogs(local_engine.log, "WARNING") as log:
            answer = self._recognize("ответ")
        self.assertNotEqual(answer["pid"], first)
        self.assertIn("between jobs", "\n".join(log.output))

    def test_engine_deaf_to_the_stop_line_is_killed(self):
        self._recognize("глухой")
        process_ = self.server._process  # noqa: SLF001
        with mock.patch.object(local_engine, "STOP_WAIT_S", 0.3), self.assertLogs(local_engine.log, "WARNING"):
            self.server.close()
        self.assertNotEqual(process_.returncode, 0)
        self.assertIsNone(self.server._process)  # noqa: SLF001


@unittest.skipUnless(importlib.util.find_spec("sherpa_onnx"), "sherpa-onnx не установлен")
class TestSherpaOnnx(unittest.TestCase):
    """The fake module matches the real one: same methods, params and fields. Catches changes when sherpa-onnx is
    updated."""

    def test_kinds_methods_and_params(self):
        so = local_engine._sherpa()
        for kind in models.KINDS.values():
            with self.subTest(kind=kind.method):
                params = inspect.signature(getattr(so.OfflineRecognizer, kind.method)).parameters
                needed_all = {*kind.files, *kind.other, "num_threads"} | ({"language"} if kind.with_language else set())
                self.assertLessEqual(needed_all, params.keys())

    def test_settings_and_fields(self):
        so = local_engine._sherpa()
        vad = so.VadModelConfig(
            silero_vad=so.SileroVadModelConfig(model="vad.onnx", max_speech_duration=20), sample_rate=SR
        )
        self.assertEqual(vad.silero_vad.window_size, 512)
        self.assertLessEqual({"start", "samples"}, set(dir(so.SpeechSegment)))
        self.assertLessEqual({"start", "end", "speaker"}, set(dir(so.OfflineSpeakerDiarizationSegment)))
        folder = pathlib.Path(tempfile.mkdtemp())
        for name in ("сегменты", "голос"):
            (folder / name).mkdir()
            (folder / name / "model.onnx").write_bytes(b"onnx")
        config = local_engine._diarization_settings(
            so,
            local_engine.Diarization(str(folder / "сегменты" / "model.onnx"), str(folder / "голос" / "model.onnx")),
            1,
        )
        self.assertEqual(config.clustering.num_clusters, -1)


def _wav(path: pathlib.Path) -> np.ndarray:
    with wave.open(str(path)) as file:
        return np.frombuffer(file.readframes(file.getnframes()), np.int16).astype(np.float32) / 32768


MODELS = pathlib.Path(os.environ.get("QG_KNOTES_MODELS", "нет такой папки"))


def _by_roles(folder: pathlib.Path) -> dict[str, str]:
    return {role: str(folder / name) for role, name in models.roles(p_.name for p_ in folder.iterdir()).items()}


def _onnx(folder: pathlib.Path) -> str:
    return str(sorted(folder.glob("*.onnx"), key=lambda path: ".int8." not in path.name)[0])


@unittest.skipUnless(MODELS.is_dir(), "нет папки настоящих моделей QG_KNOTES_MODELS")
class TestLive(unittest.TestCase):
    """Real sherpa-onnx and models from the k2-fsa/sherpa-onnx releases on GitHub. In the QG_KNOTES_MODELS folder:
    gigaam - unpacked asr-models/sherpa-onnx-nemo-ctc-punct-giga-am-v3-russian-2025-12-16.tar.bz2 (with test_wavs),
    vad - asr-models/silero_vad.onnx, сегменты - unpacked
    speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2, голос -
    speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx, and next to the folders
    speaker-segmentation-models/0-four-speakers-zh.wav; optionally whisper - asr-models/sherpa-onnx-whisper-tiny."""

    def setUp(self):
        self.model = local_engine.Model(_by_roles(MODELS / "gigaam"), "nemo_ctc", _onnx(MODELS / "vad"), "GigaAM v3")
        self.diarization = local_engine.Diarization(_onnx(MODELS / "сегменты"), _onnx(MODELS / "голос"))

    def test_gigaam_by_vad_chunks(self):
        audio = _wav(MODELS / "gigaam" / "test_wavs" / "example.wav")
        progress: list[tuple] = []
        answer = local_engine.recognize(
            np.concatenate([audio, np.zeros(3 * SR, np.float32), audio]),
            self.model,
            None,
            lambda *x_: progress.append(x_),
        )
        words = providers.words(answer)
        self.assertIn("лукоморья", answer["text"])
        starts = [s["начало"] for s in words if s["слово"].startswith("Ничьих")]
        self.assertEqual(len(starts), 2)
        self.assertAlmostEqual(starts[1] - starts[0], len(audio) / SR + 3, delta=0.5)  # time shifted by the chunk start
        self.assertEqual(sorted(words, key=lambda s: s["начало"]), words)
        self.assertEqual(progress[-1], (26, 26))

    def test_diarization_of_four_speakers(self):
        audio = _wav(MODELS / "0-four-speakers-zh.wav")
        segments = local_engine.Engine(self.model, self.diarization).diarize(audio, lambda *_: None)
        self.assertGreaterEqual(len({who for *_, who in segments}), 3)

    @unittest.skipUnless((MODELS / "whisper").is_dir(), "нет Whisper")
    def test_whisper_with_times_by_segments(self):
        model = local_engine.Model(_by_roles(MODELS / "whisper"), "whisper", _onnx(MODELS / "vad"), "Whisper", "en")
        answer = local_engine.recognize(_wav(MODELS / "whisper" / "test_wavs" / "1.wav"), model, None)
        self.assertIn("consequence", answer["text"])
        self.assertLess(answer["words"][-1]["end"], answer["duration"] + 0.01)


if __name__ == "__main__":
    unittest.main()
