# SPDX-License-Identifier: GPL-3.0-only
"""Speech recognition: joining phrases, labeling the user's own speech by tracks, merging chunks, taking in ready-made
recordings, assembling a recording."""

import json
import pathlib
import subprocess
import tempfile
import time
import tracemalloc
import unittest
from unittest import mock
import wave

import numpy as np

from knotes import recording
from knotes import recognition

SR = recognition.SAMPLE_RATE


def _noise(length: float, chunks: list[tuple[float, float, float]]) -> np.ndarray:
    """Noise of the given amplitude on the intervals (start, end, amplitude), silence elsewhere."""
    audio = np.zeros(int(length * SR), np.float32)
    for number, (start, end, amplitude) in enumerate(chunks):
        noise = np.random.default_rng(number).uniform(-amplitude, amplitude, int((end - start) * SR))
        audio[int(start * SR) : int(start * SR) + len(noise)] = noise
    return audio


def _words(*quads: tuple) -> list[dict]:
    """Words from (start, end, speaker[, text]); without a text each word is unique."""
    return [
        {"начало": ch[0], "конец": ch[1], "говорящий": ch[2], "слово": ch[3] if len(ch) > 3 else f"с{i}"}
        for i, ch in enumerate(quads)
    ]


def _who(phrases: list[dict]) -> list[str]:
    return [f["кто"] for f in phrases]


class TestPhrases(unittest.TestCase):
    def test_words_of_one_speaker_join(self):
        result = recognition.phrases(_words((0.2, 0.8, "s0"), (1.0, 1.8, "s0"), (2.2, 3.8, "s1")))
        self.assertEqual([f["текст"] for f in result], ["с0 с1", "с2"])

    def test_long_pause_splits_phrase(self):
        self.assertEqual(len(recognition.phrases(_words((0, 1, "s0"), (4, 5, "s0")))), 2)


class TestTracks(unittest.TestCase):
    def test_own_from_microphone_others_from_computer(self):
        words = {"микрофон": _words((0.2, 1.8, "m0")), "собеседники": _words((2.2, 3.8, "s0"), (4.2, 5.8, "s1"))}
        audio = {"микрофон": _noise(6, [(0, 2, 0.5)]), "собеседники": _noise(6, [(2, 6, 0.5)])}
        self.assertEqual(_who(recognition.collect_phrases(words, audio)), ["Я", "Собеседник 1", "Собеседник 2"])

    def test_own_words_over_other_speaker_not_lost(self):
        words = {"микрофон": _words((0.2, 1.0, "m0"), (3.2, 3.7, "m0")), "собеседники": _words((2.2, 3.8, "s0"))}
        audio = {"микрофон": _noise(4, [(0, 1.1, 0.5), (3.1, 3.8, 0.5)]), "собеседники": _noise(4, [(2, 4, 0.5)])}
        result = recognition.collect_phrases(words, audio)
        self.assertEqual([(f["кто"], f["начало"]) for f in result], [("Я", 0.2), ("Собеседник 1", 2.2), ("Я", 3.2)])

    def test_speaker_echo_as_separate_label_dropped(self):
        words = {
            "микрофон": _words((0.2, 1.8, "m0"), (2.2, 5.8, "m1")),
            "собеседники": _words((2.2, 3.8, "s0"), (4.2, 5.8, "s1")),
        }
        audio = {"микрофон": _noise(6, [(0, 2, 0.5), (2, 6, 0.2)]), "собеседники": _noise(6, [(2, 6, 0.5)])}
        self.assertEqual(_who(recognition.collect_phrases(words, audio)), ["Я", "Собеседник 1", "Собеседник 2"])

    def test_echo_louder_than_system_audio_decided_by_whole_speaker(self):
        # The owner is quieter than the echo; in one echo phrase the microphone is louder than the computer sound
        words = {
            "микрофон": _words((0.2, 1.8, "m1"), (2.2, 3.8, "m0"), (6.2, 7.8, "m0")),
            "собеседники": _words((2.2, 3.8, "s0"), (6.2, 7.8, "s0")),
        }
        audio = {
            "микрофон": _noise(8, [(0, 2, 0.45), (2, 4, 0.5), (4, 8, 0.1)]),
            "собеседники": _noise(8, [(2, 4, 0.4), (4, 8, 0.5)]),
        }
        self.assertEqual(_who(recognition.collect_phrases(words, audio)), ["Я", "Собеседник 1", "Собеседник 1"])

    def test_echo_in_owner_label_removed_by_text(self):
        echo = ("мы", "проверили", "отчет")

        def repeat(label: str) -> list[tuple]:
            return [(4.2 + i * 0.5, 4.6 + i * 0.5, label, s) for i, s in enumerate(echo)]

        words = {"микрофон": _words((0.2, 1.8, "m0"), *repeat("m0")), "собеседники": _words(*repeat("s0"))}
        audio = {"микрофон": _noise(6, [(0, 2, 0.5), (4, 6, 0.3)]), "собеседники": _noise(6, [(4, 6, 0.5)])}
        result = recognition.collect_phrases(words, audio)
        self.assertEqual([(f["кто"], f["начало"]) for f in result], [("Я", 0.2), ("Собеседник 1", 4.2)])

    def test_echo_search_quick_on_three_hours(self):
        """Echo is looked for only among the other participants' words near the phrase: a 3-hour meeting used to take
        7 s here, more on a weak laptop."""
        hours = 3 * 3600
        phrase = ["раз", "два", "три"]
        mic = [(t + i * 0.4, t + i * 0.4 + 0.3, "m0", s) for t in range(0, hours, 7) for i, s in enumerate(phrase)]
        others = [(t / 2, t / 2 + 0.4, "s0", f"с{t}") for t in range(2 * hours)]
        others += [(70 + i * 0.4, 70.3 + i * 0.4, "s0", s) for i, s in enumerate(phrase)]  # the echo at 70 s
        words = {"микрофон": _words(*mic), "собеседники": sorted(_words(*others), key=lambda s: -s["начало"])}
        levels = {
            "микрофон": (np.full(hours * 20, 0.5, np.float32), 0.5),
            "собеседники": (np.full(hours * 20, 0.25, np.float32), 0.5),
        }
        started = time.monotonic()
        result = recognition._collect(words, levels)
        self.assertLess(time.monotonic() - started, 1)
        mine = [f["начало"] for f in result if f["кто"] == "Я"]
        self.assertEqual(len(mine), len(range(0, hours, 7)) - 1)
        self.assertNotIn(70, mine)

    def test_without_system_audio_all_participants(self):
        words = {"микрофон": _words((0.2, 1.8, "m0"), (2.2, 3.8, "m1"), (4.2, 5.8, "m2"))}
        audio = {"микрофон": _noise(6, [(0, 6, 0.5)]), "собеседники": np.zeros(6 * SR, np.float32)}
        self.assertEqual(_who(recognition.collect_phrases(words, audio)), ["Участник 1", "Участник 2", "Участник 3"])

    def test_without_microphone_all_others(self):
        words = {"собеседники": _words((0.2, 1.8, "s0"), (2.2, 3.8, "s1"), (4.2, 5.8, "s2"))}
        audio = {"собеседники": _noise(6, [(0, 6, 0.5)])}
        self.assertEqual(
            _who(recognition.collect_phrases(words, audio)), ["Собеседник 1", "Собеседник 2", "Собеседник 3"]
        )


class TestChunks(unittest.TestCase):
    def _part(self, shift: float, *words: tuple) -> dict:
        return {
            "сдвиг": shift,
            "ответ": {
                "words": [
                    {"word": s, "start": n, "end": n + 0.4, "speaker_label": m, **d}
                    for n, s, m, *rest_ in words
                    for d in [rest_[0] if rest_ else {}]
                ]
            },
        }

    def test_labels_match_by_overlap_without_duplicates(self):
        k = recognition.CHUNK_S
        parts = [
            self._part(0, (k - 5, "один", "speaker_0"), (k + 5, "два", "speaker_0")),
            self._part(k, (5, "два", "speaker_1"), (15, "три", "speaker_1"), (30, "четыре", "speaker_1")),
        ]
        words = recognition.words_from(parts)
        self.assertEqual(
            [(s["слово"], s["говорящий"]) for s in words],
            [("один", "speaker_0"), ("два", "speaker_0"), ("три", "speaker_0"), ("четыре", "speaker_0")],
        )

    def test_unmatched_label_gets_chunk_number(self):
        k = recognition.CHUNK_S
        parts = [
            self._part(0, (10, "а", "speaker_0")),
            self._part(k, (30, "б", "speaker_0"), (31, "(смех)", "speaker_0", {"type": "audio_event"})),
        ]
        self.assertEqual(
            [(s["слово"], s["говорящий"]) for s in recognition.words_from(parts)],
            [("а", "speaker_0"), ("б", "1:speaker_0")],
        )


class TestReadyRecording(unittest.TestCase):
    def test_import_does_not_reuse_a_recorded_meeting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            existing = root / "recordings" / "2026-10-22_10-00"
            existing.mkdir(parents=True)
            (existing / "микрофон.flac").write_bytes(b"recorded")
            source = root / f"{existing.name}.mp4"
            source.touch()
            imported = recognition.folder_for_file(source, existing.parent)
            self.assertEqual(imported.name, existing.name + "_2")
            self.assertEqual(recognition.folder_for_file(source, existing.parent), imported)
            self.assertFalse((existing / recognition.SOURCE).exists())
            self.assertEqual((existing / "микрофон.flac").read_bytes(), b"recorded")

    def test_interrupted_import_can_be_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source = root / "video.mp4"
            source.touch()
            recordings = root / "recordings"

            def interrupted(*arguments):
                pathlib.Path(arguments[-1]).write_bytes(b"incomplete")
                raise OSError("interrupted")

            with mock.patch.object(recognition, "_ffmpeg", side_effect=interrupted), self.assertRaises(OSError):
                recognition.from_file(source, recordings)
            folder = recordings / "video"
            self.assertFalse((folder / "общая.flac").exists())
            self.assertFalse((folder / "общая.flac.tmp").exists())
            with mock.patch.object(
                recognition, "_ffmpeg", side_effect=lambda *args: pathlib.Path(args[-1]).write_bytes(b"audio")
            ):
                self.assertEqual(recognition.from_file(source, recordings), folder)
            self.assertEqual((folder / "общая.flac").read_bytes(), b"audio")

    def test_audio_from_video_to_shared_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            video = folder / "встреча.mp4"
            recognition._ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=160x120:rate=5:duration=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-shortest",
                str(video),
            )
            result = recognition.from_file(video, folder / "записи")
            audio = recognition.read_mono(result / "общая.flac")
            self.assertEqual(result.name, "встреча")
            self.assertAlmostEqual(len(audio) / SR, 2, delta=0.1)
            self.assertGreater(float(np.abs(audio).max()), 0.1)
            other = folder / "другое"
            other.mkdir()
            (other / "встреча.mp4").write_bytes(video.read_bytes())
            second = recognition.folder_for_file(other / "встреча.mp4", folder / "записи")
            self.assertEqual(second.name, "встреча_2")
            self.assertEqual(recognition.folder_for_file(video, folder / "записи"), result)


class TestFfmpeg(unittest.TestCase):
    def test_failure_reason_goes_to_log(self):
        """The user sees a plain text (failures.text), what ffmpeg said goes to the log."""
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertLogs(recognition.log, "WARNING") as log_,
            self.assertRaises(subprocess.CalledProcessError),
        ):
            recognition.read_mono(pathlib.Path(tmp) / "нет.flac")
        self.assertIn("No such file", "\n".join(log_.output))


def _chunk(folder: pathlib.Path, file: str, frequency: int, seconds: float, value: float) -> None:
    with wave.open(str(folder / file), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(frequency)
        w.writeframes((np.full(int(frequency * seconds), value) * 32767).astype(np.int16).tobytes())


class TestAssembly(unittest.TestCase):
    def test_long_track_joined_without_holding_it_in_memory(self):
        """A 3-hour track used to take about 2.8 GB while joining: now ffmpeg streams the chunks into the FLAC."""
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            _chunk(folder, "собеседники_01.wav", 48000, 120, 0.25)
            _chunk(folder, "собеседники_02.wav", 44100, 60, -0.25)
            manifest = [
                {"дорожка": "собеседники", "файл": "собеседники_01.wav", "начало": 0.0},
                {"дорожка": "собеседники", "файл": "собеседники_02.wav", "начало": 125.0},
            ]
            (folder / recording.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
            tracemalloc.start()
            try:
                recording.collect(folder)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertLess(peak, 2**21)  # the old way: about 45 MB for these 3 minutes
            audio = recognition.read_mono(folder / "собеседники.flac")
            self.assertAlmostEqual(len(audio) / SR, 185, delta=0.05)
            self.assertAlmostEqual(float(audio[60 * SR]), 0.25, delta=0.01)
            self.assertLess(abs(float(audio[122 * SR])), 0.001)
            self.assertAlmostEqual(float(audio[150 * SR]), -0.25, delta=0.01)

    def test_chunk_running_into_next_one_is_cut(self):
        """The device clock may run faster: a chunk longer than the time until the next one gives way to it."""
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            _chunk(folder, "микрофон_01.wav", 16000, 2, 0.5)
            _chunk(folder, "микрофон_02.wav", 16000, 1, -0.5)
            manifest = [
                {"дорожка": "микрофон", "файл": "микрофон_02.wav", "начало": 1.0},
                {"дорожка": "микрофон", "файл": "микрофон_01.wav", "начало": 0.0},
            ]
            (folder / recording.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
            recording.collect(folder)
            audio = recognition.read_mono(folder / "микрофон.flac")
            self.assertAlmostEqual(len(audio) / SR, 2, delta=0.01)
            self.assertAlmostEqual(float(audio[SR // 2]), 0.5, delta=0.01)
            self.assertAlmostEqual(float(audio[3 * SR // 2]), -0.5, delta=0.01)

    def test_chunks_after_device_change_land_on_their_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            manifest = []
            for number, (start, frequency) in enumerate([(0.0, 48000), (3.0, 44100)], 1):
                file = f"микрофон_{number:02d}.wav"
                with wave.open(str(folder / file), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(frequency)
                    w.writeframes((np.full(frequency, 0.5) * 32767).astype(np.int16).tobytes())
                manifest.append({"дорожка": "микрофон", "файл": file, "начало": start, "устройство": "тест"})
            (folder / recording.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
            recording.collect(folder)
            audio = recognition.read_mono(folder / "микрофон.flac")
            self.assertAlmostEqual(len(audio) / SR, 4, delta=0.05)
            self.assertGreater(audio[int(0.5 * SR)], 0.4)
            self.assertLess(abs(audio[int(2 * SR)]), 0.01)
            self.assertGreater(audio[int(3.5 * SR)], 0.4)
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ["микрофон.flac"])

    def test_foreign_manifest_does_not_delete_outside_files(self):
        """A manifest from a foreign or corrupted meeting folder: only this folder's own chunks are deleted and
        written."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            folder = root / "встреча"
            folder.mkdir()
            victim = root / "важный.txt"
            victim.write_text("не трогать", encoding="utf-8")
            manifest = [
                {"дорожка": "микрофон", "файл": "../важный.txt", "начало": 0.0},
                {"дорожка": "../../вне", "файл": "микрофон_01.wav", "начало": 0.0},
                "мусор",
                {"дорожка": "микрофон", "файл": "микрофон_01.wav", "начало": "0"},
            ]
            (folder / recording.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
            recording.collect(folder)
            self.assertTrue(victim.exists())
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["важный.txt", "встреча"])


if __name__ == "__main__":
    unittest.main()
