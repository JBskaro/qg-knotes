# SPDX-License-Identifier: GPL-3.0-only
"""Plays a part of a meeting: both tracks together from the given second, in a background thread so windows do not
wait."""

import logging
import pathlib
import subprocess
import threading
from collections.abc import Callable

import numpy as np
import pyaudiowpatch as pa

from knotes import failures, providers
from knotes import recognition
from knotes.language import tr

LENGTH_S = 60  # how long one part plays
log = logging.getLogger(__name__)


class Player:
    """Plays one part at a time: a new one stops the previous one. end(error) is called when the part finished on
    its own (empty error) or could not be played (what happened, to show the user)."""

    def __init__(self, audio: Callable[[], pa.PyAudio] = pa.PyAudio):
        self._audio = audio
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def playing(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def play(self, folder: pathlib.Path, start: float, end: Callable[[str], None] | None = None) -> None:
        self.stop()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._play, args=(folder, start, self._stop, end), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        self._thread = None

    def _play(
        self, folder: pathlib.Path, start: float, stop_: threading.Event, end: Callable[[str], None] | None
    ) -> None:
        result = ""
        try:
            data = recognition.audio_chunk(folder, start, LENGTH_S)
            if not data:
                result = tr("This meeting's audio is gone: it was deleted after transcription.")
            elif not stop_.is_set():
                self._output(data, stop_)
        except (OSError, subprocess.CalledProcessError, providers.ProviderError) as error:  # ProviderError: no ffmpeg
            log.warning("could not play %s from %.1f s: %s", folder.name, start, error)
            result = tr("Couldn't play. {reason}", reason=failures.text(error))
        finished = not stop_.is_set()
        stop_.set()  # playing is already False when end() runs
        if end and finished:
            end(result)

    def _output(self, data: bytes, stop_: threading.Event) -> None:
        audio = self._audio()
        try:
            stream = audio.open(format=pa.paInt16, channels=1, rate=recognition.SAMPLE_RATE, output=True)
            step = recognition.SAMPLE_RATE // 10 * 2  # 0.1 s: stopping takes effect at once
            for s in range(0, len(data), step):
                if stop_.is_set():
                    break
                stream.write(data[s : s + step])
            stream.stop_stream()
            stream.close()
        finally:
            audio.terminate()


def _tone(frequency: int, channel_count: int) -> bytes:
    """Two soft notes, 0.6 s: a chime for checking the participants' sound."""
    time = np.arange(int(frequency * 0.3)) / frequency
    fade = np.exp(-time * 6)
    audio = np.concatenate([np.sin(2 * np.pi * note_freq * time) * fade for note_freq in (660, 880)]) * 0.2
    return np.repeat((audio * 32767).astype(np.int16)[:, None], channel_count, axis=1).tobytes()


def chime(output: str = "", audio: Callable[[], pa.PyAudio] = pa.PyAudio) -> None:
    """Plays the chime in the chosen output (empty - the Windows default); the setup sees it in the loopback level."""
    audio_ = audio()
    try:
        device = next(
            (
                u
                for u in audio_.get_device_info_generator()
                if output and u["name"] == output and u["maxOutputChannels"] and not u.get("isLoopbackDevice")
            ),
            None,
        )
        if device is None:
            device = audio_.get_default_output_device_info()
        frequency, channel_count = int(device["defaultSampleRate"]), max(1, min(2, device["maxOutputChannels"]))
        stream = audio_.open(
            format=pa.paInt16, channels=channel_count, rate=frequency, output=True, output_device_index=device["index"]
        )
        stream.write(_tone(frequency, channel_count))
        stream.stop_stream()
        stream.close()
    finally:
        audio_.terminate()
