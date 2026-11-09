# SPDX-License-Identifier: GPL-3.0-only
"""Recording on fake sound: tracks, silence filling, device change, failure, ending."""

import json
import pathlib
import subprocess
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from typing import Any
from unittest import mock

import numpy as np

from knotes import recording
from knotes import recognition
from knotes import transcript

SAMPLE_RATE = 16000


class SoundStream:
    """Like a PortAudio stream: every 0.1 s passes a chunk of sound to the callback (the loudness is set)."""

    def __init__(self, channels: int, call_: Callable[..., object], loudness: float):
        self.channels, self.call_, self.loudness = channels, call_, loudness
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._go, daemon=True)
        self._thread.start()

    def _go(self) -> None:
        """As a device does: as much sound as the time that passed, so a thread woken late under load sends more."""
        start, sent = time.monotonic(), 0
        while not self._stop.wait(0.1):
            frame_count = int((time.monotonic() - start) * SAMPLE_RATE) - sent
            audio = np.full(frame_count * self.channels, int(self.loudness * 32767), np.int16)
            self.call_(audio.tobytes(), frame_count, {}, 0)
            sent += frame_count

    def is_active(self) -> bool:
        return not self._stop.is_set()

    def stop_stream(self) -> None:
        self._stop.set()
        self._thread.join()

    def close(self) -> None:
        self._stop.set()


class Sound:
    """Like PyAudio: a loopback speaker and a microphone; the number of instances is counted."""

    created = terminated = 0
    silent_output = False  # nothing plays on the computer: the loopback stream sends nothing
    all_streams: list[SoundStream] = []

    def __init__(self):
        Sound.created += 1
        self.streams: list[SoundStream] = []

    def get_default_wasapi_loopback(self) -> dict:
        return {"index": 1, "name": "динамики", "defaultSampleRate": SAMPLE_RATE, "maxInputChannels": 2}

    def get_host_api_info_by_type(self, _typ: int) -> dict:
        return {"index": 0, "defaultInputDevice": 2}

    def get_device_info_generator_by_host_api(self, host_api_index: int) -> list[dict]:
        assert host_api_index == 0
        return [
            {
                "index": 1,
                "name": "динамики [Loopback]",
                "defaultSampleRate": SAMPLE_RATE,
                "maxInputChannels": 2,
                "isLoopbackDevice": True,
            },
            {"index": 2, "name": "микрофон", "defaultSampleRate": SAMPLE_RATE, "maxInputChannels": 1},
            {"index": 3, "name": "гарнитура", "defaultSampleRate": SAMPLE_RATE, "maxInputChannels": 1},
            {
                "index": 4,
                "name": "наушники [Loopback]",
                "defaultSampleRate": SAMPLE_RATE,
                "maxInputChannels": 2,
                "isLoopbackDevice": True,
            },
            {"index": 5, "name": "наушники", "defaultSampleRate": SAMPLE_RATE, "maxInputChannels": 0},
        ]

    def get_device_info_by_index(self, _number: int) -> dict:
        return {"index": 2, "name": "микрофон", "defaultSampleRate": SAMPLE_RATE, "maxInputChannels": 1}

    def open(self, **arguments: Any) -> SoundStream:
        loudness = 0.5 if arguments["input_device_index"] == 2 else 0.25
        call_ = arguments["stream_callback"]
        if self.silent_output and arguments["input_device_index"] in (1, 4):
            call_ = lambda *_arguments: None  # noqa: E731
        stream = SoundStream(int(arguments["channels"]), call_, loudness)
        self.streams.append(stream)
        Sound.all_streams.append(stream)
        return stream

    def terminate(self) -> None:
        Sound.terminated += 1


class TestRecording(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name)
        Sound.created, Sound.terminated, Sound.silent_output, Sound.all_streams = 0, 0, False, []
        patchers = [
            mock.patch.object(recording.pa, "PyAudio", Sound),
            mock.patch.object(recording, "TICK_S", 0.1),
            mock.patch.object(recording.comtypes, "CoInitialize"),
            mock.patch.object(recording.comtypes, "CoUninitialize"),
        ]
        for z in patchers:
            z.start()
            self.addCleanup(z.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(
        self, seconds: float, defaults: Callable[[], tuple], devices: tuple[str, str] = ("", "")
    ) -> tuple[list[str], list[tuple]]:
        change_items, endings = [], []
        events = recording.Events(change_item=change_items.append, ended=lambda p, o: endings.append((p, o)))
        with mock.patch.object(recording, "defaults", defaults):
            z = recording.Recording(self.folder, events, devices)
            z.start_now()
            time.sleep(seconds)
            levels, self.names = z.levels(), z.devices()
            z.stop()
            z.wait(10)
        self.assertEqual(set(levels), {"микрофон", "собеседники"})
        return change_items, endings

    def _crash(self, exception: BaseException) -> str:
        """A recording whose devices fail at once: it must end with a message, not hang."""
        endings = []
        events = recording.Events(change_item=lambda _t: None, ended=lambda _p, o: endings.append(o))
        with (
            mock.patch.object(Sound, "get_default_wasapi_loopback", side_effect=exception),
            mock.patch.object(recording, "defaults", lambda: (None, None)),
            self.assertLogs(recording.log, "ERROR"),
        ):
            z = recording.Recording(self.folder, events)
            z.start_now()
            z.wait(10)
        self.assertEqual(len(endings), 1)
        return endings[0]

    def test_no_headphones_or_device_gone(self):
        self.assertIn("Не найдены наушники или колонки", self._crash(LookupError("no loopback")))
        self.assertIn("Звуковое устройство недоступно", self._crash(OSError(-9996, "Invalid device")))
        self.assertIn("Непредвиденная ошибка", self._crash(RuntimeError("странно")))

    def test_chosen_devices_and_replacing_missing(self):
        change_items, endings = self._write(0.5, lambda: ("д", "м"), ("гарнитура", "наушники"))
        self.assertEqual(
            (change_items, self.names), ([], {"микрофон": "гарнитура", "собеседники": "наушники [Loopback]"})
        )
        self.assertEqual(endings[0][1], "")
        change_items, _ = self._write(0.5, lambda: ("д", "м"), ("старая гарнитура", "колонки"))
        self.assertEqual(
            change_items,
            [
                "Выбранный вывод «колонки» не найден, пишу звук, выбранный в Windows",
                "Выбранный микрофон «старая гарнитура» не найден, пишу микрофон, выбранный в Windows",
            ],
        )
        self.assertEqual(self.names, {"микрофон": "микрофон", "собеседники": "динамики"})

    def test_device_list_for_settings(self):
        self.assertEqual(recording.device_list(), (["микрофон", "гарнитура"], ["динамики", "наушники"]))

    def test_recording_assembles_two_tracks(self):
        change_items, endings = self._write(0.8, lambda: ("д", "м"))
        self.assertEqual(change_items, [])
        self.assertEqual(self.names, {"микрофон": "микрофон", "собеседники": "динамики"})
        self.assertEqual(endings, [(self.folder, "")])
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["микрофон.flac", "собеседники.flac"])
        microphone = recognition.read_mono(self.folder / "микрофон.flac")
        self.assertAlmostEqual(len(microphone) / SAMPLE_RATE, 0.8, delta=0.25)
        self.assertGreater(float(microphone[len(microphone) // 2]), 0.4)

    def test_device_change_reopens_recording(self):
        start = time.monotonic()

        def defaults() -> tuple[str, str]:
            return ("д2", "м") if time.monotonic() - start > 0.3 else ("д1", "м")

        with mock.patch.object(recording.transcript, "write_json", wraps=recording.transcript.write_json) as written:
            change_items, endings = self._write(1.0, defaults)
        self.assertEqual(Sound.created, 2)
        self.assertEqual(change_items, ["Запись перешла на другие устройства"])
        self.assertEqual(written.call_count, 2)  # the manifest is replaced whole, a crash does not cut it off
        self.assertEqual(endings, [(self.folder, "")])
        audio = recognition.read_mono(self.folder / "собеседники.flac")
        self.assertAlmostEqual(len(audio) / SAMPLE_RATE, 1.0, delta=0.3)

    def test_manifest_written_while_recording(self):
        z = recording.Recording(self.folder)
        with mock.patch.object(recording, "defaults", lambda: ("д", "м")):
            z.start_now()
            time.sleep(0.6)
            manifest = json.loads((self.folder / recording.MANIFEST).read_text(encoding="utf-8"))
            z.stop()
            z.wait(10)
        self.assertEqual({k["дорожка"] for k in manifest}, {"микрофон", "собеседники"})
        self.assertGreater(z.duration(), 0.5)

    def test_pause_not_in_recording_and_marks(self):
        z = recording.Recording(self.folder)
        with mock.patch.object(recording, "defaults", lambda: ("д", "м")):
            z.start_now()
            time.sleep(0.5)
            z.pause(True)
            self.assertTrue(z.paused)
            time.sleep(0.6)
            paused = z.duration()
            z.pause(False)
            time.sleep(0.4)
            label = z.mark()
            z.stop()
            z.wait(10)
        self.assertAlmostEqual(paused, 0.5, delta=0.15)
        self.assertAlmostEqual(label, 0.9, delta=0.2)
        audio = recognition.read_mono(self.folder / "собеседники.flac")
        self.assertAlmostEqual(len(audio) / SAMPLE_RATE, 0.9, delta=0.3)  # without the 0.6 s pause
        self.assertEqual(len(transcript.labels(self.folder)), 1)

    def test_assembly_failure_still_ends_recording(self):
        """Any failure while assembling the tracks ends the recording with a plain message: the app does not hang on
        "Assembling recording", and the ffmpeg command line does not reach the user."""
        command = subprocess.CalledProcessError(1, ["ffmpeg", "-i", "секретная_папка"], stderr=b"broken")
        for failure, text in (
            (RuntimeError("No ffmpeg exe could be found"), "Непредвиденная ошибка"),
            (MemoryError(), "Непредвиденная ошибка"),
            (PermissionError(13, "Permission denied", "микрофон.flac"), "Нет доступа (микрофон.flac)"),
            (command, "Не удалось обработать звук"),
        ):
            endings: list[tuple] = []
            events = recording.Events(ended=lambda p, o, e=endings: e.append((p, o)))
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.object(recording, "defaults", lambda: ("д", "м")),
                mock.patch.object(recording, "collect", side_effect=failure),
                self.assertLogs(recording.log, "ERROR"),
            ):
                z = recording.Recording(self.folder, events)
                z.start_now()
                time.sleep(0.3)
                z.stop()
                z.wait(10)
                self.assertEqual(len(endings), 1)
                self.assertIn("Не удалось собрать запись", endings[0][1])
                self.assertIn(text, endings[0][1])
                self.assertNotIn("секретная_папка", endings[0][1])

    def test_microphone_refused_closes_computer_audio(self):
        """Windows privacy settings block the microphone: the computer audio stream opened before it is closed with
        its PyAudio, and nothing keeps writing after "Recording stopped"."""
        real_open = Sound.open

        def open_(sound: Sound, **arguments: Any) -> SoundStream:
            if arguments["input_device_index"] == 2:
                raise OSError(-9999, "Unanticipated host error")
            return real_open(sound, **arguments)

        endings = []
        events = recording.Events(ended=lambda _p, o: endings.append(o))
        with (
            mock.patch.object(Sound, "open", open_),
            mock.patch.object(recording, "defaults", lambda: ("д", "м")),
            self.assertLogs(recording.log, "ERROR"),
        ):
            z = recording.Recording(self.folder, events)
            z.start_now()
            z.wait(10)
        self.assertEqual(len(endings), 1)
        self.assertIn("Звуковое устройство недоступно", endings[0])
        self.assertEqual(len(Sound.all_streams), 1)
        self.assertFalse(Sound.all_streams[0].is_active())
        self.assertEqual(Sound.terminated, Sound.created)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_pause_while_devices_reopen(self):
        """A pause pressed while the devices reopen (100-300 ms) pauses the new tracks too."""
        start, real_open = time.monotonic(), Sound.open
        z = recording.Recording(self.folder)

        def open_(sound: Sound, **arguments: Any) -> SoundStream:
            stream = real_open(sound, **arguments)
            if Sound.created == 2 and arguments["input_device_index"] == 2:
                z.pause(True)
            return stream

        def defaults() -> tuple[str, str]:
            return ("д2", "м") if time.monotonic() - start > 0.3 else ("д1", "м")

        with mock.patch.object(Sound, "open", open_), mock.patch.object(recording, "defaults", defaults):
            z.start_now()
            while not z.paused and time.monotonic() - start < 5:
                time.sleep(0.05)
            time.sleep(0.3)
            with z._lock:
                before = {name: d.recorded_amount for name, d in z._tracks.items()}
            time.sleep(0.5)
            with z._lock:
                after = {name: d.recorded_amount for name, d in z._tracks.items()}
            z.pause(False)
            z.stop()
            z.wait(10)
        self.assertEqual(set(before), {"микрофон", "собеседники"})
        self.assertEqual(before, after)

    def test_track_that_fails_to_close_does_not_leave_others_open(self):
        """The disk filled up while closing the microphone file: the computer audio is still closed, PyAudio
        terminated, the user gets a message."""
        endings = []
        events = recording.Events(ended=lambda _p, o: endings.append(o))
        real = recording.Track.close_view

        def close_view(track: recording.Track) -> None:
            real(track)
            if track.path.name.startswith("микрофон"):
                raise OSError(28, "No space left on device")

        with (
            mock.patch.object(recording.Track, "close_view", close_view),
            mock.patch.object(recording, "defaults", lambda: ("д", "м")),
            self.assertLogs(recording.log, "ERROR"),
        ):
            z = recording.Recording(self.folder, events)
            z.start_now()
            time.sleep(0.3)
            z.stop()
            z.wait(10)
        self.assertEqual(len(endings), 1)
        self.assertIn("На диске закончилось место", endings[0])
        self.assertTrue(all(not stream.is_active() for stream in Sound.all_streams))
        self.assertEqual(Sound.terminated, Sound.created)

    def test_silence_written_as_it_goes(self):
        """While nothing plays, the computer audio track gets its silence every second, not as one huge piece at the
        pause or the stop (an hour of it froze the window and took a gigabyte)."""
        Sound.silent_output = True
        z = recording.Recording(self.folder)
        with mock.patch.object(recording, "defaults", lambda: ("д", "м")):
            z.start_now()
            time.sleep(1.2)
            size = (self.folder / "собеседники_01.wav").stat().st_size
            z.stop()
            z.wait(10)
        self.assertGreater(size, 0.6 * SAMPLE_RATE * 2)
        audio = recognition.read_mono(self.folder / "собеседники.flac")
        self.assertAlmostEqual(len(audio) / SAMPLE_RATE, 1.2, delta=0.3)

    def test_long_gap_filled_by_blocks(self):
        start = time.monotonic()
        track = recording.Track(Sound(), Sound().get_default_wasapi_loopback(), self.folder / "проба.wav", start, True)
        track.stream.stop_stream()
        writes = []
        real = track.file.writeframes
        with mock.patch.object(track.file, "writeframes", lambda data: writes.append(len(data)) or real(data)):
            track.pause(True, start + 3.5)
        self.assertEqual(sum(writes), int(3.5 * SAMPLE_RATE) * 2)
        self.assertLessEqual(max(writes), SAMPLE_RATE * 2)
        track.close_view()

    def test_gone_device_still_closes_file(self):
        """The headset was pulled out: its stream does not stop, but the chunk file is closed whole."""
        file = self.folder / "проба.wav"
        track = recording.Track(Sound(), Sound().get_default_wasapi_loopback(), file, time.monotonic(), fill=True)
        time.sleep(0.3)
        track.stream.stop_stream()
        with (
            mock.patch.object(track.stream, "stop_stream", side_effect=OSError(-9988, "Stream closed")),
            self.assertLogs(recording.log, "WARNING"),
        ):
            track.close_view()
        self.assertTrue(track.disk.closed)
        self.assertEqual(track._accept_file(bytes(3200), 800, {}, 0), (None, recording.pa.paComplete))
        self.assertGreater(len(recognition.read_mono(file)) / SAMPLE_RATE, 0.15)

    def test_filling_system_audio_silence(self):
        file = self.folder / "проба.wav"
        track = recording.Track(Sound(), Sound().get_default_wasapi_loopback(), file, time.monotonic(), fill=True)
        track.stream.stop_stream()
        time.sleep(0.5)
        track.close_view()
        self.assertGreater(len(recognition.read_mono(file)) / SAMPLE_RATE, 0.4)


if __name__ == "__main__":
    unittest.main()
