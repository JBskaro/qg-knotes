# SPDX-License-Identifier: GPL-3.0-only
"""Screen recording: what to record (meeting window area, its screen, a monitor), quality, encoder fallback,
chunks on pause, a failure that leaves the audio alone, and joining real short chunks with audio tracks
(ffmpeg test pattern, the screen is not captured)."""

import json
import pathlib
import re
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

from knotes import recording, screen, system
from knotes import recognition
from tests.audio_fixture import write_flac

PRIMARY_MONITOR = screen.Monitor(r"\\.\DISPLAY1", 0, 0, 1920, 1080, True, handle=101)
SECOND = screen.Monitor(r"\\.\DISPLAY2", 1920, -1080, 3840, 2160, handle=202)
WHOLE_PRIMARY = screen.Capture(PRIMARY_MONITOR)


class FakeChunk:
    """Stands in for one ffmpeg process: starts or not, as the test says."""

    launches: list[str] = []  # encoder of each launch
    successful: set[str] = set()

    def __init__(self, command: list[str], folder: pathlib.Path):
        encoder = command[command.index("-c:v") + 1]
        self.launches.append(encoder)
        self.started = threading.Event()
        self.frames_at_start, self.frames = 5, 30
        if encoder in self.successful:
            self.started.set()
            (folder / command[-1]).write_bytes(b"mkv")
        self.process = mock.Mock(poll=lambda: None)

    def finish(self) -> str:
        return ""


class TestChoice(unittest.TestCase):
    def test_what_to_record(self):
        all_, window = [SECOND, PRIMARY_MONITOR], ((2000, -900, 1280, 720), SECOND)
        self.assertEqual(
            screen.choose_capture(screen.MEETING_WINDOW, ["Teams.exe"], "высокое", all_, lambda _p: window),
            screen.Capture(SECOND, (2000, -900, 1280, 720), "высокое"),
        )
        self.assertEqual(
            screen.choose_capture(screen.MEETING_SCREEN, ["Teams.exe"], "эконом", all_, lambda _p: window),
            screen.Capture(SECOND, (), "эконом"),
        )
        # no meeting window: the whole screen is not recorded instead, it has what the user did not want to show
        self.assertIsNone(screen.choose_capture(screen.MEETING_WINDOW, ["Teams.exe"], "обычное", all_, lambda _p: None))
        self.assertEqual(screen.choose_capture(screen.PRIMARY, [], "обычное", all_), WHOLE_PRIMARY)
        self.assertEqual(screen.choose_capture(SECOND.device, [], "обычное", all_).monitor, SECOND)
        self.assertEqual(screen.choose_capture("нет такого", [], "непонятное", all_), WHOLE_PRIMARY)

    def test_monitors_of_this_computer(self):
        all_ = screen.monitors()
        self.assertTrue(all_)
        self.assertEqual(sum(m.primary for m in all_), 1)
        self.assertTrue(all(m.handle for m in all_))

    def test_window_area_within_monitor_and_even(self):
        """A window past the monitor edge is captured within it; sizes are even, otherwise the encoder fails."""

        def dwm(_window: int, _what: int, rect: object, _size_of: int) -> int:
            p_ = rect._obj
            p_.left, p_.top, p_.right, p_.bottom = -15, 100, 1001, 677
            return 0

        with mock.patch.object(screen.ctypes.windll.dwmapi, "DwmGetWindowAttribute", dwm):
            self.assertEqual(screen._window_area(1, PRIMARY_MONITOR), (0, 100, 1000, 576))

    def test_commands_for_window_monitor_and_quality(self):
        window = " ".join(
            screen.capture_command(
                screen.Capture(PRIMARY_MONITOR, (100, 50, 1280, 720), "эконом"), "экран_01.mkv", "h264_mf"
            )
        )
        self.assertIn("-framerate 8 -draw_mouse 1 -offset_x 100 -offset_y 50 -video_size 1280x720 -i desktop", window)
        self.assertIn("-vf scale=-2:'min(ih,720)',format=nv12 -c:v h264_mf -b:v 600k", window)
        monitor = screen.capture_command(screen.Capture(SECOND, (), "высокое"), "экран_01.mkv", "libx264")
        text = " ".join(monitor)
        self.assertIn("-framerate 15 -draw_mouse 1 -offset_x 1920 -offset_y -1080 -video_size 3840x2160", text)
        self.assertIn("-vf format=yuv420p -c:v libx264 -preset veryfast -crf 26", text)  # high quality: as on screen
        self.assertEqual((monitor[0], monitor[-1]), (system.ffmpeg(), "экран_01.mkv"))


class TestScreenRecording(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name)
        FakeChunk.launches, FakeChunk.successful = [], {"libx264"}
        patchers = [
            mock.patch.object(screen, "_Chunk", FakeChunk),
            mock.patch.object(screen, "FIRST_FRAME_TIMEOUT_S", 0.05),
        ]
        for z in patchers:
            z.start()
            self.addCleanup(z.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_fallback_encoder_and_chunks_on_pause(self):
        clock = iter([10.0, 14.0, 25.0, 29.0])  # opened, closed on pause, opened again, stopped
        screen_capture = screen.ScreenRecorder(self.folder, WHOLE_PRIMARY, lambda: next(clock))
        with self.assertLogs(screen.log, "WARNING"):
            screen_capture.start()
            screen_capture.pause(True)
            screen_capture.pause(False)
            screen_capture.stop()
        # the hardware encoder did not start: software one, no extra attempts after that
        self.assertEqual(FakeChunk.launches, ["h264_mf", "libx264", "libx264"])
        # start is the stop moment minus 30 frames (3 s at 10 frames per second), not the first ffmpeg report
        self.assertEqual(
            screen.read_chunks(self.folder),
            [{"файл": "экран_02.mkv", "начало": 11.0}, {"файл": "экран_03.mkv", "начало": 26.0}],
        )
        self.assertFalse((self.folder / "экран_01.mkv").exists())
        self.assertEqual(screen_capture.error, "")

    def test_screen_fails_audio_continues(self):
        FakeChunk.successful = set()
        messages = []
        screen_capture = screen.ScreenRecorder(
            self.folder, screen.Capture(PRIMARY_MONITOR, (0, 0, 640, 480)), lambda: 0.0, messages.append
        )
        with self.assertLogs(screen.log, "WARNING"):
            screen_capture.start()
            screen_capture.stop()
        self.assertEqual(
            messages, ["Экран не записывается: ни один способ записи экрана не сработал. Звук пишется как обычно."]
        )
        self.assertEqual(screen.read_chunks(self.folder), [])

    def test_no_ffmpeg_stops_only_the_video(self):
        """ffmpeg is missing (an antivirus took it to quarantine): the screen badge goes off with a plain message, the
        screen thread does not die silently."""
        missing = (system.FfmpegMissing(system.FFMPEG_MISSING), FileNotFoundError(2, "No such file", "ffmpeg.exe"))
        for failure in missing:
            messages = []
            screen_capture = screen.ScreenRecorder(self.folder, WHOLE_PRIMARY, lambda: 0.0, messages.append)
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.object(screen, "_Chunk", side_effect=failure),
                self.assertLogs(screen.log, "WARNING"),
            ):
                screen_capture.start()
                screen_capture.pause(True)
                screen_capture.pause(False)
                screen_capture.stop()
            self.assertEqual(len(messages), 1)
            self.assertTrue(messages[0].startswith("Экран не записывается: "))
            self.assertEqual(screen_capture.error, messages[0])
        self.assertIn("ffmpeg не найден", system.FfmpegMissing(system.FFMPEG_MISSING).args[0].lower())

    def test_foreign_names_in_manifest_not_taken(self):
        (self.folder / screen.CHUNK_LIST).write_text(
            json.dumps(
                [
                    {"файл": "..\\запись.mkv", "начало": 0},
                    {"файл": "экран_01.mkv", "начало": "x"},
                    {"файл": "экран_02.mkv", "начало": 1},
                ]
            ),
            encoding="utf-8",
        )
        self.assertEqual(screen.read_chunks(self.folder), [{"файл": "экран_02.mkv", "начало": 1}])

    def test_chunk_list_replaced_whole(self):
        """The chunk list is written to a new file put in place: a crash mid-write does not cut it off."""
        screen_capture = screen.ScreenRecorder(self.folder, WHOLE_PRIMARY, lambda: 0.0)
        screen_capture._add_chunk("экран_01.mkv", 0.0)
        first = (self.folder / screen.CHUNK_LIST).stat().st_ino
        screen_capture._add_chunk("экран_02.mkv", 5.0)
        self.assertNotEqual((self.folder / screen.CHUNK_LIST).stat().st_ino, first)
        self.assertEqual([c["файл"] for c in screen.read_chunks(self.folder)], ["экран_01.mkv", "экран_02.mkv"])

    def test_chunk_list_with_pause(self):
        self.assertEqual(
            screen.concat_list([{"файл": "экран_01.mkv", "начало": 0.5}, {"файл": "экран_02.mkv", "начало": 4.0}]),
            "file 'экран_01.mkv'\nduration 3.500\nfile 'экран_02.mkv'\n",
        )


class TestAssembly(unittest.TestCase):
    """Real ffmpeg on generated chunks: the video lands at its start time and carries both voices."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _chunk(self, name: str, seconds: float) -> None:
        subprocess.run(
            [
                system.ffmpeg(),
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=size=320x240:rate=10:duration={seconds}",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(self.folder / name),
            ],
            check=True,
            capture_output=True,
        )

    def _duration(self, file: pathlib.Path) -> tuple[float, str]:
        output = subprocess.run(
            [system.ffmpeg(), "-hide_banner", "-i", str(file)],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        ).stderr
        ch, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", output).groups()
        return int(ch) * 3600 + int(m) * 60 + float(s), output

    def test_chunks_and_audio_into_one_video(self):
        self._chunk("экран_01.mkv", 1)
        self._chunk("экран_02.mkv", 1)
        (self.folder / screen.CHUNK_LIST).write_text(
            json.dumps([{"файл": "экран_01.mkv", "начало": 0.5}, {"файл": "экран_02.mkv", "начало": 2.0}]),
            encoding="utf-8",
        )
        tone = (
            0.3 * np.sin(np.arange(4 * recognition.SAMPLE_RATE) / recognition.SAMPLE_RATE * 2 * np.pi * 440)
        ).astype(np.float32)
        for track in ("микрофон", "собеседники"):
            write_flac(tone, self.folder / f"{track}.flac")
        screen.assemble(self.folder)
        video = self.folder / screen.VIDEO_FILE
        duration, info = self._duration(video)
        self.assertAlmostEqual(duration, 4.0, delta=0.3)  # audio is longer than video: the video does not cut it
        self.assertIn("Video: h264", info)
        self.assertIn("Audio: aac", info)
        self.assertEqual(
            sorted(p.name for p in self.folder.iterdir()), ["видео.mp4", "микрофон.flac", "собеседники.flac"]
        )

    def test_failed_assembly_keeps_chunks(self):
        (self.folder / "экран_01.mkv").write_bytes(b"not a video")
        (self.folder / screen.CHUNK_LIST).write_text(
            json.dumps([{"файл": "экран_01.mkv", "начало": 0}]), encoding="utf-8"
        )
        with self.assertRaises(subprocess.CalledProcessError):
            screen.assemble(self.folder)
        self.assertTrue((self.folder / "экран_01.mkv").exists())
        self.assertFalse((self.folder / screen.VIDEO_FILE).exists())

    def test_interrupted_recording_with_video_is_found(self):
        (self.folder / "встреча").mkdir()
        (self.folder / "встреча" / screen.CHUNK_LIST).write_text("[]", encoding="utf-8")
        self.assertEqual(recording.interrupted_all(self.folder), [self.folder / "встреча"])


class TestInRecording(unittest.TestCase):
    def test_recording_drives_screen(self):
        with_screen = mock.Mock()
        with mock.patch.object(screen, "ScreenRecorder", return_value=with_screen):
            z = recording.Recording(pathlib.Path(tempfile.gettempdir()), screen_capture=WHOLE_PRIMARY)
            z.started_at = time.monotonic()
            z.pause(True)
            z.pause(False)
        self.assertEqual(with_screen.pause.call_args_list, [mock.call(True), mock.call(False)])
        no_screen = recording.Recording(pathlib.Path(tempfile.gettempdir()))
        self.assertIsNone(no_screen._screen)


if __name__ == "__main__":
    unittest.main()
