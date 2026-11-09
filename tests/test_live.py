# SPDX-License-Identifier: GPL-3.0-only
"""Transcript during the meeting: new audio of the recording chunks is recognized in pieces by the engine on this
computer (one run for both tracks, no speaker diarization) in one engine process for the whole recording: the model
loads once; the recording files stay as they are, a piece ends at a quiet moment, the next one continues from there;
echo of the speakers in the microphone is dropped; a failure is logged, shown in plain text and never stops the
recording, a crashed engine starts again with a later piece; a question goes to the minutes model with the part
already recognized, its cost to the spending log."""

import dataclasses
import json
import os
import pathlib
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import wave
from unittest import mock

import numpy as np

from knotes import catalog, live, local_engine, minutes, models, providers, recognition, spending, transcript
from knotes import settings
from knotes.language import tr

SR = recognition.SAMPLE_RATE


def _download(folder: pathlib.Path, model: models.Model) -> None:
    """A model as after a download: files of the right size and the marker."""
    place = folder / model.id
    place.mkdir(parents=True, exist_ok=True)
    for file in model.files:
        (place / file.name).write_bytes(b"\0" * min(file.size, 16))
    label = {f.name: {"размер": (place / f.name).stat().st_size, "sha256": f.sha256} for f in model.files}
    (place / models.MARKER).write_text(json.dumps({"файлы": label}), encoding="utf-8")


def _sound(seconds: float, bursts: list[tuple[float, float, float]], rate: int = 48000) -> np.ndarray:
    """Silence with "speech" (a 440 Hz tone) from..to seconds at the given loudness."""
    audio = np.zeros(int(seconds * rate), np.float32)
    for start, end, loudness in bursts:
        moments = np.arange(int(start * rate), int(end * rate))
        audio[moments] = loudness * np.sin(2 * np.pi * 440 * moments / rate)
    return audio


def _pcm(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()


def _chunk(folder: pathlib.Path, name: str, start: float, audio: np.ndarray, rate: int = 48000) -> None:
    """A chunk as the recording writes it: a 16-bit mono WAV "<track>_NN.wav" and its line in the manifest."""
    file, track = f"{name}.wav", name.rpartition("_")[0]
    with wave.open(str(folder / file), "wb") as recording_file:
        recording_file.setnchannels(1)
        recording_file.setsampwidth(2)
        recording_file.setframerate(rate)
        recording_file.writeframes(_pcm(audio))
    manifest = transcript.read_json(folder / transcript.MANIFEST, [])
    manifest.append({"дорожка": track, "файл": file, "начало": start, "устройство": "тест"})
    transcript.write_json(folder / transcript.MANIFEST, manifest)


def _write_more(path: pathlib.Path, audio: np.ndarray) -> None:
    """The recording goes on: frames are appended, the header sizes are not updated yet."""
    with path.open("ab") as file:
        file.write(_pcm(audio))


def _runs(audio: np.ndarray, rate: int) -> list[tuple[float, float]]:
    """Loud stretches of the audio in seconds; quiet gaps under 0.3 s do not split them."""
    window = rate // 20
    levels = np.sqrt((audio[: len(audio) // window * window].reshape(-1, window) ** 2).mean(axis=1))
    result: list[list[float]] = []
    for number in np.flatnonzero(levels > 0.02):
        start, end = number / 20, (number + 1) / 20
        if result and start - result[-1][1] < 0.3:
            result[-1][1] = end
        else:
            result.append([start, end])
    return [(a, b) for a, b in result]


class FakeEngine:
    """Instead of the engine process: hears the loud stretches of the job file and says the next queued phrase over
    each of them."""

    def __init__(self, *speech: str):
        self.speech = list(speech)
        self.calls: list[dict] = []

    def __call__(self, path, _model, diarization, _progress=None, cancel=None) -> dict:
        with wave.open(str(path)) as file:
            rate = file.getframerate()
            audio = np.frombuffer(file.readframes(file.getnframes()), np.int16).astype(np.float32) / 32768
        self.calls.append({"rate": rate, "seconds": len(audio) / rate, "diarization": diarization, "cancel": cancel})
        words = []
        for start, end in _runs(audio, rate):
            said = self.speech.pop(0).split()
            step = (end - start) / len(said)
            words += [
                {"start": start + n * step, "end": start + (n + 1) * step - 0.05, "word": w, "speaker": "speaker_0"}
                for n, w in enumerate(said)
            ]
        return {"text": "", "duration": len(audio) / rate, "words": words}


# The engine process for the test: the real local_engine.main() with the model replaced. Each load of the model adds a
# line to the file of the first argument ("сбой" as the second one: the engine crashes while loading); the model says
# "слово<N>" over each loud stretch, N counts on through the life of the process
ENGINE = textwrap.dedent("""
    import os, sys, wave
    import numpy as np
    from knotes import local_engine

    class Model:
        said = 0

        def __init__(self, model, diarization):
            with open(sys.argv[1], "a", encoding="utf-8") as loads:
                loads.write(f"{model.title} {diarization}\\n")
            if sys.argv[2:] == ["сбой"]:
                os._exit(3)

        def cut(self, audio):
            window = 800
            loud = np.abs(audio[: len(audio) // window * window]).reshape(-1, window).max(axis=1) > 0.02
            start = None
            for number, on in enumerate([*loud, False]):
                if on and start is None:
                    start = number
                elif not on and start is not None:
                    yield start * window / 16000, audio[start * window : number * window]
                    start = None

        def recognize(self, chunk):
            Model.said += 1
            return "", [(f" слово{Model.said}", 0.0, len(chunk) / 16000)]

    def read(path):
        with wave.open(path) as file:
            return np.frombuffer(file.readframes(file.getnframes()), np.int16).astype(np.float32) / 32768

    local_engine.Engine, local_engine._read = Model, read
    sys.exit(local_engine.main(sys.argv))
""")


class TestLive(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.models = root / "модели"
        self.folder = root / "записи" / "2026-10-26_10-00"
        self.folder.mkdir(parents=True)
        self.seconds = 30.0
        tiny = [
            models.dataclasses.replace(m, files=tuple(models.dataclasses.replace(f, size=16) for f in m.files))
            for m in models.catalog()
        ]
        patcher = mock.patch.object(models, "catalog", lambda: tiny)
        patcher.start()
        self.addCleanup(patcher.stop)
        for model in tiny:
            _download(self.models, model)
        self.params = recognition.Params(providers.find(providers.LOCAL), "", models_folder=self.models)

    def tearDown(self):
        self._tmp.cleanup()

    def _live(self, config: settings.Settings | None = None) -> live.Live:
        return live.Live(self.folder, self.params, lambda: self.seconds, config or settings.Settings())

    def _meeting(self) -> None:
        """30 s of a call: I speak at 2-4 s, the other side at 6-9 s."""
        _chunk(self.folder, "микрофон_01", 0.0, _sound(30, [(2, 4, 0.3)]))
        _chunk(self.folder, "собеседники_01", 0.0, _sound(30, [(6, 9, 0.3)]))

    def test_both_tracks_in_one_engine_run_become_phrases_by_time(self):
        self._meeting()
        before = {p.name: p.read_bytes() for p in self.folder.iterdir()}
        engine = FakeEngine("добрый день коллеги", "пришлем смету до пятницы")
        flow = self._live()
        with mock.patch.object(local_engine.Server, "recognize", side_effect=engine):
            self.assertTrue(flow.step())
        self.assertEqual(len(engine.calls), 1)  # the model loads once per piece, not once per track
        call = engine.calls[0]
        self.assertEqual(call["rate"], SR)
        self.assertIsNone(call["diarization"])  # no speaker diarization: the piece is too short and it is slower
        self.assertIs(call["cancel"], flow.params.cancel)
        phrases = flow.phrases()
        self.assertEqual(
            [(p.track, p.text) for p in phrases],
            [
                ("микрофон", "добрый день коллеги"),
                ("собеседники", "пришлем смету до пятницы"),
            ],
        )
        self.assertAlmostEqual(phrases[0].start, 2, delta=0.2)
        self.assertAlmostEqual(phrases[1].start, 6, delta=0.2)
        self.assertTrue(flow.others_heard)
        self.assertEqual([live.who(p, flow.others_heard) for p in phrases], ["Я", "Собеседники"])
        self.assertGreater(flow.done_until, 25)  # the quietest moment near the end: here, the silence at the end
        self.assertLessEqual(flow.done_until, self.seconds - live.BEHIND_S)
        self.assertEqual({p.name: p.read_bytes() for p in self.folder.iterdir()}, before)  # the recording untouched
        self.assertEqual(flow.error, "")

    def test_next_piece_waits_for_audio_and_goes_on_from_the_cut(self):
        self._meeting()
        engine = FakeEngine("добрый день коллеги", "пришлем смету до пятницы", "акт подпишем в понедельник")
        flow = self._live()
        with mock.patch.object(local_engine.Server, "recognize", side_effect=engine):
            flow.step()
            cut = flow.done_until
            self.seconds = cut + live.BEHIND_S + live.MIN_PIECE_S - 1
            self.assertFalse(flow.step())  # too little new audio: waits for the next round
            self.assertEqual(len(engine.calls), 1)
            _write_more(self.folder / "микрофон_01.wav", _sound(30, []))
            _write_more(self.folder / "собеседники_01.wav", _sound(30, [(10, 12.5, 0.3)]))  # 40-42.5 s of the meeting
            self.seconds = 60.0
            self.assertTrue(flow.step())
        last = flow.phrases()[-1]
        self.assertEqual((last.track, last.text), ("собеседники", "акт подпишем в понедельник"))
        self.assertAlmostEqual(last.start, 40, delta=0.2)
        self.assertLess(engine.calls[1]["seconds"], 60 - cut)  # only the new audio, without the silent microphone

    def test_chunks_after_device_change_and_other_sample_rates(self):
        _chunk(self.folder, "микрофон_01", 0.0, _sound(10, [(2, 4, 0.3)], 44100), 44100)
        _chunk(self.folder, "микрофон_02", 12.0, _sound(18, [(3, 5, 0.3)], 16000), 16000)  # 15-17 s of the meeting
        engine = FakeEngine("слышно меня", "переключил гарнитуру")
        flow = self._live()
        with mock.patch.object(local_engine.Server, "recognize", side_effect=engine):
            flow.step()
        starts = [round(p.start) for p in flow.phrases()]
        self.assertEqual(starts, [2, 15])
        self.assertFalse(flow.others_heard)  # no computer audio: an in-person meeting, nobody is "me"
        self.assertEqual(live.who(flow.phrases()[0], flow.others_heard), "Участники")

    def test_echo_of_speakers_in_microphone_is_dropped(self):
        _chunk(self.folder, "микрофон_01", 0.0, _sound(30, [(2, 4, 0.3), (8, 11, 0.05)]))  # my words, then the echo
        _chunk(self.folder, "собеседники_01", 0.0, _sound(30, [(8, 11, 0.3)]))
        engine = FakeEngine("добрый день коллеги", "пришлем смету до пятницы", "пришлем смету до пятницы")
        flow = self._live()
        with mock.patch.object(local_engine.Server, "recognize", side_effect=engine):
            flow.step()
        self.assertEqual(
            [(p.track, p.text) for p in flow.phrases()],
            [
                ("микрофон", "добрый день коллеги"),
                ("собеседники", "пришлем смету до пятницы"),
            ],
        )

    def test_vocabulary_fixes_phrases(self):
        self._meeting()
        engine = FakeEngine("смотрим один эс", "да один эс")
        config = settings.Settings(vocabulary=[{"правильно": "1С", "варианты": ["один эс"]}])
        self.params = dataclasses.replace(recognition.params(config), models_folder=self.models)
        flow = self._live()
        with mock.patch.object(local_engine.Server, "recognize", side_effect=engine):
            flow.step()
        self.assertEqual([p.text for p in flow.phrases()], ["смотрим 1С", "да 1С"])

    def test_silence_needs_no_engine(self):
        _chunk(self.folder, "микрофон_01", 0.0, _sound(30, []))
        _chunk(self.folder, "собеседники_01", 0.0, _sound(30, []))
        flow = self._live()
        with mock.patch.object(local_engine.Server, "recognize") as engine:
            self.assertTrue(flow.step())
        engine.assert_not_called()
        self.assertGreater(flow.done_until, 25)
        self.assertEqual(flow.phrases(), [])

    def test_nothing_recorded_yet(self):
        flow = self._live()
        self.assertFalse(flow.step())  # no manifest: the first chunk is not open yet
        (self.folder / "микрофон_01.wav").write_bytes(b"")  # opened, the header is not written yet
        transcript.write_json(
            self.folder / transcript.MANIFEST, [{"дорожка": "микрофон", "файл": "микрофон_01.wav", "начало": 0}]
        )
        with mock.patch.object(local_engine.Server, "recognize") as engine:
            flow.step()
        engine.assert_not_called()
        self.assertEqual(flow.status(), tr("Phrases appear here about half a minute after they are said"))

    def test_foreign_manifest_lines_are_ignored(self):
        _chunk(self.folder, "микрофон_01", 0.0, _sound(30, [(2, 4, 0.3)]))
        manifest = transcript.read_json(self.folder / transcript.MANIFEST, [])
        manifest += [
            {"дорожка": "микрофон", "файл": "../чужой_01.wav", "начало": 0},
            {"дорожка": "общая", "файл": "общая_01.wav", "начало": 0},
            "строка",
        ]
        transcript.write_json(self.folder / transcript.MANIFEST, manifest)
        engine = FakeEngine("только мое")
        with mock.patch.object(local_engine.Server, "recognize", side_effect=engine):
            self._live().step()
        self.assertEqual(len(engine.calls), 1)

    def test_failure_is_logged_shown_and_skipped(self):
        self._meeting()
        flow = self._live()
        broken = providers.ProviderError("Модель «GigaAM» повреждена.")
        with (
            mock.patch.object(local_engine.Server, "recognize", side_effect=broken),
            self.assertLogs("knotes.live", "WARNING"),
        ):
            self.assertTrue(flow.step())
        self.assertEqual(flow.error, "Модель «GigaAM» повреждена.")
        self.assertEqual(flow.status(), flow.error)
        self.assertGreater(flow.done_until, 25)  # the piece is skipped: the next round does not hammer the same audio
        self.assertFalse(flow.params.cancel.is_set())

    def test_unexpected_failure_in_plain_text_and_gives_up_after_several(self):
        self._meeting()
        flow = self._live()
        with (
            mock.patch.object(local_engine.Server, "recognize", side_effect=RuntimeError("сбой")),
            self.assertLogs("knotes.live", "ERROR"),
        ):
            for round_ in range(live.MAX_FAILURES):
                self.seconds = 30.0 + 30 * round_
                _write_more(self.folder / "собеседники_01.wav", _sound(30, [(5, 8, 0.3)]))
                _write_more(self.folder / "микрофон_01.wav", _sound(30, []))
                flow.step()
        self.assertTrue(flow.params.cancel.is_set())  # stopped for this meeting; the recording goes on by itself
        reason = tr("Unexpected error: {error}. Details are in the app log.", error="сбой")
        self.assertEqual(
            flow.error, tr("Transcript during the meeting stopped: {reason} The recording goes on.", reason=reason)
        )

    def test_stop_cancels_the_engine_quietly(self):
        self._meeting()
        flow = self._live()

        def cancelled(_path, _model, _diarization, _progress=None, cancel=None):
            self.assertIs(cancel, flow.params.cancel)
            flow.stop()
            raise local_engine.Cancelled(tr(local_engine.CANCELLED_TEXT))

        with mock.patch.object(local_engine.Server, "recognize", side_effect=cancelled):
            flow.step()
        self.assertEqual(flow.error, "")
        self.assertEqual(flow.phrases(), [])

    def test_background_thread_recognizes_and_stops(self):
        self._meeting()
        engine = FakeEngine("добрый день коллеги", "пришлем смету до пятницы")
        flow = self._live()
        with (
            mock.patch.object(live, "PERIOD_S", 0.01),
            mock.patch.object(live, "REST_S", 0.01),
            mock.patch.object(local_engine.Server, "recognize", side_effect=engine),
            mock.patch.object(local_engine.Server, "close") as close,
        ):
            flow.start()
            deadline = time.monotonic() + 10
            while not flow.phrases() and time.monotonic() < deadline:
                time.sleep(0.01)
            flow.stop()
            flow.wait(5)
        self.assertEqual(len(flow.phrases()), 2)
        self.assertFalse(flow.running)
        close.assert_called_once()  # the thread ends the engine process on its way out

    def _engine_process(self, loads: pathlib.Path, *arguments: str):
        """The test engine process (ENGINE) instead of the program."""
        command = [sys.executable, "-c", ENGINE, str(loads), *arguments]
        return mock.patch.object(
            local_engine.system, "startup", return_value=(command, {"PYTHONPATH": os.pathsep.join(sys.path)})
        )

    def _more(self, round_: int) -> None:
        """30 more seconds of the call: I speak at 5-7 s of them, the other side at 10-12 s."""
        _write_more(self.folder / "микрофон_01.wav", _sound(30, [(5, 7, 0.3)]))
        _write_more(self.folder / "собеседники_01.wav", _sound(30, [(10, 12, 0.3)]))
        self.seconds = 30.0 * (round_ + 1)

    def test_model_loads_once_for_the_whole_recording(self):
        """Pieces go to one engine process: the model loads with the first piece and stays loaded, the model counts
        its words on in the same process; the stop of the recording ends the process by the stop line."""
        loads = pathlib.Path(self._tmp.name) / "загрузки.txt"
        self._meeting()
        flow = self._live()
        self.addCleanup(flow.stop)
        with self._engine_process(loads):
            for round_ in range(3):
                if round_:
                    self._more(round_)
                self.assertTrue(flow.step())
                self.assertEqual(flow.error, "")
        lines = loads.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)  # one load for three pieces
        self.assertTrue(lines[0].endswith(" None"))  # without speaker diarization
        self.assertEqual(
            [(p.track, p.text) for p in flow.phrases()],
            [("микрофон", "слово1"), ("собеседники", "слово2")]
            + [("микрофон", "слово3"), ("собеседники", "слово4")]
            + [("микрофон", "слово5"), ("собеседники", "слово6")],
        )
        self.assertAlmostEqual(flow.phrases()[-1].start, 70, delta=0.2)
        process_ = flow.params.engine._process  # noqa: SLF001
        flow.stop()
        self.assertEqual(process_.returncode, 0)  # it exited by itself, it was not killed
        self.assertIsNone(flow.params.engine._process)  # noqa: SLF001

    def test_engine_ends_when_the_program_is_gone(self):
        """The program died: the input of the engine closes, and the engine process exits by itself."""
        self._meeting()
        flow = self._live()
        self.addCleanup(flow.stop)
        with self._engine_process(pathlib.Path(self._tmp.name) / "загрузки.txt"):
            flow.step()
        process_ = flow.params.engine._process  # noqa: SLF001
        process_.stdin.close()
        self.assertEqual(process_.wait(30), 0)

    def test_crashed_engine_in_plain_text_started_again_given_up_after_several(self):
        loads = pathlib.Path(self._tmp.name) / "загрузки.txt"
        self._meeting()
        flow = self._live()
        self.addCleanup(flow.stop)
        title = tr(models.find(self.params.final_model, self.models).title)
        reason = tr(local_engine.ENGINE_FELL, title=title, code="0x3")
        with (
            self._engine_process(loads, "сбой"),
            mock.patch.object(local_engine, "RESTART_S", 0),
            self.assertLogs("knotes", "WARNING"),
        ):
            for round_ in range(live.MAX_FAILURES):
                if round_:
                    self._more(round_)
                self.assertTrue(flow.step())
                if round_ == 0:
                    self.assertEqual(flow.error, reason)
                    self.assertEqual(flow.status(), reason)
                    self.assertFalse(flow.params.cancel.is_set())
        started = loads.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(started), live.MAX_FAILURES)  # a new process after each crash
        self.assertTrue(flow.params.cancel.is_set())  # stopped for this meeting; the recording goes on by itself
        self.assertEqual(
            flow.error, tr("Transcript during the meeting stopped: {reason} The recording goes on.", reason=reason)
        )
        self.assertIsNone(flow.params.engine._process)  # noqa: SLF001

    def test_starts_only_when_on_and_on_this_computer(self):
        clock = lambda: 0.0  # noqa: E731
        self.assertIsNone(live.start(settings.Settings(), self.folder, clock))  # off by default
        cloud = settings.Settings(live_transcript=True, provider="openrouter")
        self.assertIsNone(live.start(cloud, self.folder, clock))
        with mock.patch.object(threading.Thread, "start") as thread:
            flow = live.start(settings.Settings(live_transcript=True), self.folder, clock)
        thread.assert_called_once()
        self.assertIsInstance(flow, live.Live)
        self.assertTrue(flow.params.provider.local_engine)
        self.assertFalse(flow.params.diarization)
        self.assertEqual(flow.status(), tr("Phrases appear here about half a minute after they are said"))
        with (
            mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("can't start new thread")),
            self.assertLogs("knotes.live", "ERROR"),
        ):
            self.assertIsNone(live.start(settings.Settings(live_transcript=True), self.folder, clock))

    def test_reading_chunks_that_are_not_ready(self):
        path = self.folder / "микрофон_01.wav"
        with self.assertLogs("knotes.live", "WARNING"):
            self.assertEqual(live.read_wav(path, 0, 5).size, 0)  # gone: the recording was assembled
        path.write_bytes(b"RIFF\0\0\0\0WAVEfmt \x10\0\0\0" + bytes(16))  # the header is cut before the data
        self.assertEqual(live.read_wav(path, 0, 5).size, 0)
        _chunk(self.folder, "собеседники_01", 0.0, _sound(3, [(0, 3, 0.3)]))
        self.assertEqual(live.read_wav(self.folder / "собеседники_01.wav", 4, 2).size, 0)
        self.assertEqual(len(live.read_wav(self.folder / "собеседники_01.wav", 2, 5)), SR)  # only what is written
        self.assertEqual(live.quiet_cut([np.zeros(100, np.float32)]), 100)  # shorter than 50 ms: the end

    def test_resampling_keeps_time_and_pitch(self):
        tone = _sound(2, [(0, 2, 0.5)], 44100)
        result = live.to_16k(tone, 44100)
        self.assertEqual(len(result), 2 * SR)
        spectrum = np.abs(np.fft.rfft(result))
        self.assertAlmostEqual(np.argmax(spectrum) * SR / len(result), 440, delta=2)
        hiss = np.sin(2 * np.pi * 12000 * np.arange(48000) / 48000).astype(np.float32)
        filtered = live.to_16k(hiss, 48000)[20:-20]  # the filter has no neighbors at the very edges
        self.assertLess(np.abs(filtered).max(), 0.01)  # above 8 kHz: filtered out, not folded into speech
        self.assertIs(live.to_16k(result, SR), result)


class TestQuestion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name) / "2026-10-26_10-00"
        self.folder.mkdir()
        self.flow = live.Live(
            self.folder,
            recognition.Params(providers.find(providers.LOCAL), ""),
            lambda: 600.0,
            settings.Settings(my_name="Анна", minutes_model_id="модель-протокола"),
        )
        self.flow.others_heard, self.flow.done_until = True, 70.0
        self.flow._phrases = [  # noqa: SLF001 - as if two pieces were already recognized
            live.Phrase(6.0, 9.0, "собеседники", "Пришлем смету до пятницы."),
            live.Phrase(63.0, 65.0, "микрофон", "Хорошо, жду."),
        ]

    def tearDown(self):
        self._tmp.cleanup()

    def test_question_goes_to_minutes_model_with_minutes_and_cost(self):
        with (
            mock.patch.object(settings, "address_key", return_value="ключ"),
            mock.patch.object(providers, "model_answer", return_value=("Смету до пятницы [00:06].\n", 0.0012)) as ask,
        ):
            answer = self.flow.ask("что клиент обещал прислать?")
        self.assertEqual(answer, "Смету до пятницы [00:06].")
        service, key, body = ask.call_args.args
        self.assertEqual((service.id, key, body["model"]), ("openrouter", "ключ", "модель-протокола"))
        self.assertEqual(body["provider"], {"data_collection": "deny"})
        system, user = body["messages"]
        self.assertEqual(system, {"role": "system", "content": live.PROMPT})
        self.assertIn("[00:06] Собеседники: Пришлем смету до пятницы.\n[01:03] Я: Хорошо, жду.", user["content"])
        self.assertIn("Имя владельца - Анна", user["content"])
        self.assertTrue(user["content"].endswith("Вопрос - что клиент обещал прислать?"))
        expenses = spending.list_(self.folder.parent, [])
        self.assertEqual(
            [(e.meeting, e.kind, e.model, e.how_many) for e in expenses],
            [(self.folder.name, catalog.MINUTES_KIND, "модель-протокола", 0.0012)],
        )

    def test_long_meeting_sends_the_latest_part(self):
        self.flow._phrases = [  # noqa: SLF001
            live.Phrase(float(n), n + 0.5, "собеседники", f"фраза {n} " + "слово " * 20) for n in range(3000)
        ]
        with (
            mock.patch.object(settings, "address_key", return_value="ключ"),
            mock.patch.object(providers, "model_answer", return_value=("Нет ответа.", 0.0)) as ask,
        ):
            self.flow.ask("что было в конце?")
        content = ask.call_args.args[2]["messages"][1]["content"]
        self.assertLess(len(content), live.QUESTION_CHARS + 500)
        self.assertIn("фраза 2999 ", content)
        self.assertNotIn("фраза 0 ", content)
        self.assertEqual(spending.list_(self.folder.parent, []), [])  # nothing paid, nothing logged

    def test_personal_data_hidden_from_a_cloud_model(self):
        self.flow._phrases.append(live.Phrase(70.0, 72.0, "собеседники", "Звоните на +7 913 555-12-34."))  # noqa: SLF001
        with (
            mock.patch.object(settings, "address_key", return_value="ключ"),
            mock.patch.object(providers, "model_answer", return_value=("По [ТЕЛЕФОН_1] [01:10].", 0.001)) as ask,
        ):
            answer = self.flow.ask("куда звонить?")
        content = ask.call_args.args[2]["messages"][1]["content"]
        self.assertNotIn("555-12-34", content)
        self.assertIn("[ТЕЛЕФОН_1]", content)
        self.assertEqual(answer, "По +7 913 555-12-34 [01:10].")

    def test_answer_kept_when_the_spending_log_is_not_written(self):
        with (
            mock.patch.object(settings, "address_key", return_value="ключ"),
            mock.patch.object(providers, "model_answer", return_value=("Ответ.", 0.01)),
            mock.patch.object(spending, "write", side_effect=PermissionError(13, "занят")),
            self.assertLogs("knotes.live", "WARNING"),
        ):
            self.assertEqual(self.flow.ask("вопрос?"), "Ответ.")

    def test_no_key_no_model_nothing_recognized(self):
        with self.assertRaises(providers.ProviderError) as refusal:
            self.flow.ask("вопрос?")
        self.assertEqual(
            str(refusal.exception),
            tr("No API key for {service}, set it in the Minutes section in Settings", service="OpenRouter"),
        )
        with (
            mock.patch.object(settings, "address_key", return_value="ключ"),
            mock.patch.object(minutes, "params") as params,
        ):
            params.return_value = minutes.Params(providers.find("свой", "протоколы"), "", "", "инструкция")
            with self.assertRaises(providers.ProviderError):
                self.flow.ask("вопрос?")
        self.flow._phrases = []  # noqa: SLF001
        with (
            mock.patch.object(settings, "address_key", return_value="ключ"),
            self.assertRaises(providers.ProviderError) as refusal,
        ):
            self.flow.ask("вопрос?")
        self.assertEqual(str(refusal.exception), tr("Nothing has been recognized yet. Ask again in half a minute."))

    def test_answer_minutes_become_links_to_phrases(self):
        html = live.answer_html("Смету <до пятницы> [00:06], акт [1:02:03].\nВсе.")
        self.assertIn('<a href="6">[00:06]</a>', html)
        self.assertIn('<a href="3723">[1:02:03]</a>', html)
        self.assertIn("&lt;до пятницы&gt;", html)
        self.assertIn("<br>", html)
        phrases = self.flow.phrases()
        self.assertEqual(live.nearest(phrases, 6), 0)
        self.assertEqual(live.nearest(phrases, 60), 1)
        self.assertIsNone(live.nearest([], 60))

    def test_lines_for_the_notepad(self):
        phrase = self.flow.phrases()[0]
        self.assertEqual(live.display_line(phrase, True), "00:06  Собеседники: Пришлем смету до пятницы.")
        self.assertEqual(live.note_text(phrase, True), "Собеседники: Пришлем смету до пятницы.")
        self.assertEqual(self.flow.status(), tr("Recognized up to {time}", time="01:10"))


if __name__ == "__main__":
    unittest.main()
