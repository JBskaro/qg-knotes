# SPDX-License-Identifier: GPL-3.0-only
"""Playing a meeting place on a fake sound output: plays to the end, stops, stays silent without audio."""

import pathlib
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np

from knotes import language, player, system
from knotes import recognition
from tests.audio_fixture import write_flac


class Output:
    """Like a PortAudio output stream: collects what is written, writes slowly, like a real sound card."""

    def __init__(self, delay: float):
        self.recorded_amount, self.delay, self.closed = bytearray(), delay, False

    def write(self, data: bytes) -> None:
        self.recorded_amount += data
        threading.Event().wait(self.delay)

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class Sound:
    def __init__(self, delay: float = 0.0):
        self.output, self.finished = Output(delay), False

    def open(self, **params: object) -> Output:
        assert params["output"] and params["rate"] == recognition.SAMPLE_RATE
        return self.output

    def terminate(self) -> None:
        self.finished = True


class TestPlayer(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name)
        write_flac(np.full(3 * recognition.SAMPLE_RATE, 0.3, np.float32), self.folder / "микрофон.flac")

    def tearDown(self):
        self._tmp.cleanup()

    def _end(self) -> tuple[threading.Event, list[str]]:
        end, errors = threading.Event(), []
        self.mark = lambda error: (errors.append(error), end.set())
        return end, errors

    def test_plays_place_to_end(self):
        audio = Sound()
        end, errors = self._end()
        audio_player = player.Player(lambda: audio)
        audio_player.play(self.folder, 1.0, self.mark)
        self.assertTrue(end.wait(10))
        self.assertEqual(errors, [""])
        self.assertAlmostEqual(len(audio.output.recorded_amount) / 2 / recognition.SAMPLE_RATE, 2.0, delta=0.05)
        self.assertTrue(audio.output.closed and audio.finished)
        self.assertFalse(audio_player.playing)

    def test_stop(self):
        audio = Sound(delay=0.05)
        end, _ = self._end()
        audio_player = player.Player(lambda: audio)
        audio_player.play(self.folder, 0.0, self.mark)
        for _ in range(100):
            if audio.output.recorded_amount:
                break
            threading.Event().wait(0.02)
        self.assertTrue(audio_player.playing)
        audio_player.stop()
        self.assertFalse(audio_player.playing)
        self.assertFalse(end.is_set())
        self.assertLess(len(audio.output.recorded_amount) / 2 / recognition.SAMPLE_RATE, 3.0)
        self.assertTrue(audio.finished)

    def test_without_audio_and_on_failure(self):
        end, errors = self._end()
        audio_player = player.Player(Sound)
        audio_player.play(self.folder / "нет", 0.0, self.mark)
        self.assertTrue(end.wait(5))
        self.assertIn("удалили", errors[-1])
        end.clear()
        with (
            mock.patch.object(recognition, "audio_chunk", side_effect=OSError(-9996, "Invalid output device")),
            self.assertLogs(player.log, "WARNING"),
        ):
            audio_player.play(self.folder, 0.0, self.mark)
            self.assertTrue(end.wait(5))
        self.assertIn("Звуковое устройство недоступно", errors[-1])

    def test_texts_in_interface_language_and_no_ffmpeg(self):
        end, errors = self._end()
        audio_player = player.Player(Sound)
        try:
            language.set_("en")
            audio_player.play(self.folder / "нет", 0.0, self.mark)
            self.assertTrue(end.wait(5))
        finally:
            language.set_("ru")
        self.assertEqual(errors[-1], "This meeting's audio is gone: it was deleted after transcription.")
        end.clear()
        with (
            mock.patch.object(recognition, "audio_chunk", side_effect=system.FfmpegMissing(system.FFMPEG_MISSING)),
            self.assertLogs(player.log, "WARNING"),
        ):
            audio_player.play(self.folder, 0.0, self.mark)
            self.assertTrue(end.wait(5))
        self.assertEqual(errors[-1], f"Не удалось проиграть. {system.FFMPEG_MISSING}")


if __name__ == "__main__":
    unittest.main()
