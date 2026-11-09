# SPDX-License-Identifier: GPL-3.0-only
"""Meeting video in the meeting view: hidden without a video, a still frame of a moment, frames while a phrase
plays, late frames of an older request dropped; the strip shows when the screen is recorded; the settings rows."""

import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QComboBox, QFormLayout, QLabel

from knotes import providers, transcript, screen, system
from knotes import settings
from knotes.ui import settings_window
from knotes.ui.meeting_view import MeetingView
from knotes.ui.recording_bar import RecordingBar
from knotes.ui.video_view import HEIGHT, WIDTH, VideoView

program = QApplication.instance() or QApplication([])


def _wait_for(condition, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        program.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False


class TestMeetingVideo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)  # last: by then ffmpeg has released the video
        self.folder = pathlib.Path(self._tmp.name)

    def _kind(self) -> VideoView:
        kind = VideoView(self.folder)
        self.addCleanup(kind.deleteLater)
        self.addCleanup(kind._pool.waitForDone)
        return kind

    def _video(self) -> None:
        subprocess.run(
            [
                system.ffmpeg(),
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=10:duration=3",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(self.folder / screen.VIDEO_FILE),
            ],
            check=True,
            capture_output=True,
        )

    def test_hidden_without_video(self):
        kind = self._kind()
        self.assertTrue(kind.isHidden())

    def test_frame_of_place_and_frames_while_playing(self):
        self._video()
        kind = self._kind()
        self.assertFalse(kind.isHidden())
        self.assertTrue(_wait_for(lambda: kind.pixmap() is not None and not kind.pixmap().isNull()))
        self.assertEqual((kind.pixmap().width(), kind.pixmap().height()), (WIDTH, HEIGHT))
        frames = []
        kind._frame.connect(lambda _generation, frame: frames.append(frame))
        kind.play(0.5, 1.0)
        self.assertTrue(_wait_for(lambda: len(frames) >= 4))
        kind.stop()
        time.sleep(0.3)  # the playback thread stops ffmpeg

    def test_late_frame_of_old_request_dropped(self):
        kind = self._kind()
        old_value = kind._generation
        kind.stop()
        frame = QImage(WIDTH, HEIGHT, QImage.Format.Format_RGB888)
        kind._show(old_value, frame)
        self.assertTrue(kind.pixmap() is None or kind.pixmap().isNull())

    def test_meeting_with_video_shows_it(self):
        transcript.save(
            self.folder,
            [{"начало": 1.0, "конец": 2.0, "кто": "Я", "текст": "Смотрим."}],
            {"длительность": 3, "провайдер": "x", "стоимость": 0},
            [],
        )
        window = MeetingView(self.folder)
        self.addCleanup(window.close)
        self.addCleanup(window.video._pool.waitForDone)
        self.assertTrue(window.video.isHidden())
        self._video()
        window.refresh()  # the video was assembled after recording
        self.assertFalse(window.video.isHidden())


class TestRecordingBar(unittest.TestCase):
    def test_screen_icon(self):
        line = RecordingBar(hide_from_capture=False)
        self.addCleanup(line.deleteLater)
        self.assertTrue(line._screen.isHidden())
        line.show_screen_icon(True)
        self.assertFalse(line._screen.isHidden())
        line.collapse(True)
        self.assertTrue(line._screen.isHidden())
        line.collapse(False)
        self.assertFalse(line._screen.isHidden())
        line.show_screen_icon(False)  # the screen failed to start, audio keeps recording
        self.assertTrue(line._screen.isHidden())


class TestScreenSettings(unittest.TestCase):
    def test_what_to_record_and_quality_shown_only_with_screen_recording(self):
        monitors = [screen.Monitor("D1", 0, 0, 1920, 1080, True), screen.Monitor("D2", 1920, 0, 1920, 1080)]
        for patcher in (
            mock.patch.object(screen, "monitors", return_value=monitors),
            mock.patch.object(providers, "models", return_value=[]),  # the cloud model list without network
            mock.patch.object(settings, "key", return_value=""),
            mock.patch.object(settings, "address_key", return_value=""),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        window = settings_window.SettingsWindow(settings.Settings())
        self.addCleanup(window.deleteLater)
        form_layout = window.pages.widget(2).layout()
        self.assertIsInstance(form_layout, QFormLayout)
        lines = {
            form_layout.itemAt(n_, QFormLayout.ItemRole.LabelRole).widget().text(): n_
            for n_ in range(form_layout.rowCount())
            if isinstance(
                form_layout.itemAt(n_, QFormLayout.ItemRole.LabelRole)
                and form_layout.itemAt(n_, QFormLayout.ItemRole.LabelRole).widget(),
                QLabel,
            )
        }
        self.assertIn("Удалять звук и видео", lines)
        what, quality = (
            form_layout.itemAt(lines[i], QFormLayout.ItemRole.FieldRole).widget() for i in ("Что снимать", "Качество")
        )
        self.assertEqual([form_layout.isRowVisible(s) for s in (what, quality)], [False, False])
        line = form_layout.itemAt(lines["Записывать экран"], QFormLayout.ItemRole.FieldRole).widget()
        switch = line.findChild(settings_window.style.Switch)
        switch.click()
        self.assertTrue(window.settings.record_screen)
        self.assertEqual([form_layout.isRowVisible(s) for s in (what, quality)], [True, True])
        list_ = what.findChild(QComboBox)
        self.assertEqual(
            [list_.itemText(n_) for n_ in range(list_.count())],
            ["Окно встречи", "Экран с окном встречи", "Основной монитор", "Монитор 2"],
        )
        quality.selected.emit("высокое")
        self.assertEqual(window.settings.video_quality, "высокое")


if __name__ == "__main__":
    unittest.main()
