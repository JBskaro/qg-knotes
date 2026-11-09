# SPDX-License-Identifier: GPL-3.0-only
"""Meeting video above the transcript: a still frame of the chosen moment, frames in step with the played phrase."""

import logging
import pathlib
import subprocess
import threading
import time

from PySide6.QtCore import QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QLabel

from knotes import screen, system
from knotes.ui.background import run_in_background

WIDTH, HEIGHT = 640, 360
PLAY_FPS = 5
log = logging.getLogger(__name__)
_FIT = f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2"


def frame_command(video: pathlib.Path, seconds: float) -> list[str]:
    return [
        system.ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{seconds:.2f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        _FIT,
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "pipe:1",
    ]


def stream_command(video: pathlib.Path, seconds: float, length: float) -> list[str]:
    return [
        system.ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{seconds:.2f}",
        "-t",
        f"{length:.2f}",
        "-i",
        str(video),
        "-an",
        "-vf",
        f"fps={PLAY_FPS},{_FIT}",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def read_frame(command: list[str]) -> bytes | None:
    """PNG of one frame; None if ffmpeg could not read it (the video is gone or broken), logged without a trace."""
    try:
        return subprocess.run(command, capture_output=True, check=True, creationflags=_no_window()).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        log.warning("video frame: %s", getattr(error, "stderr", b"")[-300:] or error)
        return None


class VideoView(QLabel):
    """Hidden when the meeting has no video. Each request gets a generation number: a late frame of an older
    request is dropped."""

    _frame = Signal(int, QImage)

    def __init__(self, folder: pathlib.Path):
        super().__init__()
        self.video = folder / screen.VIDEO_FILE
        self.setFixedSize(WIDTH, HEIGHT)
        self.setStyleSheet("background: #000000; border-radius: 6px;")
        self._generation = 0
        self._stop = threading.Event()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._frame.connect(self._show)
        self.reload()

    def reload(self) -> None:
        """Show or hide by whether the video exists (it appears once the recording is assembled)."""
        self.setVisible(self.video.is_file())
        self.show_frame(0)

    def show_frame(self, seconds: float) -> None:
        if not self.video.is_file():
            return
        self.stop()
        generation, command = self._generation, frame_command(self.video, seconds)
        run_in_background(self._pool, lambda: read_frame(command), lambda result: self._show_still(generation, result))

    def play(self, seconds: float, length: float) -> None:
        """Frames from seconds for length seconds, timed to start together with the audio player."""
        if not self.video.is_file():
            return
        self.stop()
        self._stop = threading.Event()
        threading.Thread(target=self._stream, args=(seconds, length, self._generation, self._stop), daemon=True).start()

    def stop(self) -> None:
        self._generation += 1
        self._stop.set()

    def _show_still(self, generation: int, result: object) -> None:
        if isinstance(result, bytes):
            self._show(generation, QImage.fromData(result))

    def _show(self, generation: int, image: QImage) -> None:
        if generation == self._generation and not image.isNull():
            self.setPixmap(QPixmap.fromImage(image))

    def _stream(self, seconds: float, length: float, generation: int, stop: threading.Event) -> None:
        size = WIDTH * HEIGHT * 3
        with subprocess.Popen(
            stream_command(self.video, seconds, length),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=_no_window(),
        ) as process:
            try:
                start, number = time.monotonic(), 0
                while not stop.is_set() and len(data := process.stdout.read(size)) == size:
                    if stop.wait(max(0.0, start + number / PLAY_FPS - time.monotonic())):
                        break
                    self._frame.emit(
                        generation, QImage(data, WIDTH, HEIGHT, WIDTH * 3, QImage.Format.Format_RGB888).copy()
                    )
                    number += 1
            except RuntimeError:  # the view was closed while playing
                log.debug("video view closed during playback")
            finally:
                process.kill()
