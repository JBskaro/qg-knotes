# SPDX-License-Identifier: GPL-3.0-only
"""Meeting recording: the microphone and the computer audio as two tracks.

The devices are the Windows defaults. If they change in the middle of a meeting (a headset was plugged in, Windows
switched the audio), recording reopens on the new ones in chunks; on assembly the chunks take their own time.
An interrupted recording (the app was closed, a reboot) stays with its manifest and is assembled later.
"""

import contextlib
import dataclasses
import datetime
import logging
import os
import pathlib
import re
import subprocess
import threading
import time
import wave
from collections.abc import Callable

import comtypes
import numpy as np
import pyaudiowpatch as pa
from pycaw.pycaw import AudioUtilities

from knotes import catalog, failures, screen
from knotes import recognition
from knotes import transcript
from knotes.language import tr

MANIFEST = transcript.MANIFEST
TICK_S = 1.0
CHECK_TICKS = 2  # in ticks
FLUSH_TICKS = 5  # in ticks
log = logging.getLogger(__name__)


class Track:
    """Writes the device stream to a 16-bit mono WAV.

    The computer audio (loopback) is silent while nothing plays; such gaps are filled with silence by the clock from
    the chunk start so the tracks match in time. Nothing is written during a pause, and after it the clock shifts by
    the pause length, so the recording has no pause in it.
    """

    def __init__(self, audio: pa.PyAudio, device: dict, path: pathlib.Path, started_at: float, fill: bool):
        """A device that does not open (busy, blocked by the Windows privacy settings) leaves no open or empty
        file."""
        self.frequency = int(device["defaultSampleRate"])
        self.channels = device["maxInputChannels"]
        self.path, self.started_at, self.fill, self.recorded_amount, self.peak = path, started_at, fill, 0, 0
        self.paused_since: float | None = None
        self.closed = False
        self._recording = threading.Lock()  # written by the audio thread and (on pause and close) the recording thread
        with contextlib.ExitStack() as opening:
            opening.callback(path.unlink, missing_ok=True)  # runs last, after the file is closed
            self.disk = opening.enter_context(path.open("wb"))
            self.file = opening.enter_context(wave.open(self.disk, "wb"))
            self.file.setnchannels(1)
            self.file.setsampwidth(2)
            self.file.setframerate(self.frequency)
            self.stream = audio.open(
                format=pa.paInt16,
                channels=self.channels,
                rate=self.frequency,
                input=True,
                input_device_index=device["index"],
                frames_per_buffer=self.frequency // 10,
                stream_callback=self._accept_file,
            )
            opening.pop_all()

    def _pad_to(self, moment: float) -> None:
        """Silence up to the moment, a second at a time: a long gap does not become one huge write."""
        shortage = int((moment - self.started_at) * self.frequency) - self.recorded_amount
        if not self.fill or shortage <= self.frequency // 4:
            return
        second = bytes(2 * self.frequency)
        while shortage > 0:
            part = min(shortage, self.frequency)
            self.file.writeframes(second[: 2 * part])
            self.recorded_amount += part
            shortage -= part

    def catch_up(self, moment: float) -> None:
        """Silence while nothing plays is written as it goes, so a pause or the stop has only a second to add."""
        with self._recording:
            if self.paused_since is None:
                self._pad_to(moment)

    def _accept_file(self, data: bytes, frame_count: int, _time: dict, _status_flags: int) -> tuple[None, int]:
        mono = np.frombuffer(data, np.int16).reshape(-1, self.channels).mean(axis=1).astype(np.int16)
        with self._recording:
            if self.closed:
                return None, pa.paComplete
            if self.paused_since is not None:
                return None, pa.paContinue
            self._pad_to(time.monotonic() - frame_count / self.frequency)
            self.file.writeframes(mono.tobytes())
            self.recorded_amount += frame_count
        self.peak = max(self.peak, int(np.abs(mono).max(initial=0)))
        return None, pa.paContinue

    def pause(self, on: bool, moment: float) -> None:
        with self._recording:
            if on and self.paused_since is None:
                self._pad_to(moment)
                self.paused_since = moment
            elif not on and self.paused_since is not None:
                self.started_at += moment - self.paused_since
                self.paused_since = None

    def clear_peak(self) -> int:
        peak, self.peak = self.peak, 0
        return peak

    def alive(self) -> bool:
        return self.stream.is_active()

    def reset(self) -> None:
        """Flush to disk so a crash loses no more than a few seconds."""
        self.disk.flush()
        os.fsync(self.disk.fileno())

    def close_view(self) -> None:
        """Stops the stream and closes the file; the file is closed even if the device is already gone."""
        try:
            self.stream.stop_stream()
            self.stream.close()
        except OSError:
            log.warning("the %s stream did not stop: the device is probably gone", self.path.name, exc_info=True)
        with self._recording:
            self.closed = True
            try:
                self._pad_to(self.paused_since or time.monotonic())
            finally:
                try:
                    self.file.close()
                finally:
                    self.disk.close()


def _close_all(tracks: list[Track], audio: pa.PyAudio | None) -> Exception | None:
    """Closes every track even if one fails, then PyAudio; returns the first failure, all go to the log."""
    failure = None
    for d in tracks:
        try:
            d.close_view()
        except Exception as e:  # the other tracks still have to be closed
            log.exception("closing %s", d.path.name)
            failure = failure or e
    if audio:
        audio.terminate()
    return failure


def _manifest(folder: pathlib.Path) -> list[dict]:
    """Recording chunks from the manifest. Only our own: an app track and a "<track>_NN.wav" file in this folder, so a
    foreign or damaged manifest cannot point to deleting or overwriting a file somewhere else."""
    path = folder / MANIFEST
    data = transcript.read_json(path, [])
    return [
        k
        for k in data
        if isinstance(k, dict)
        and k.get("дорожка") in recognition.TRACKS
        and isinstance(k.get("начало"), int | float)
        and re.fullmatch(rf"{re.escape(k['дорожка'])}_\d{{2,}}\.wav", str(k.get("файл", "")))
    ]


LOOPBACK = " [Loopback]"  # PyAudioWPatch's name for recording what plays on an output device


def _wasapi_devices(audio: pa.PyAudio) -> list[dict]:
    wasapi = audio.get_host_api_info_by_type(pa.paWASAPI)
    return list(audio.get_device_info_generator_by_host_api(host_api_index=wasapi["index"]))


def device_list() -> tuple[list[str], list[str]]:
    """Microphones and audio outputs (headphones, speakers, monitors) by name, to pick in Settings."""
    audio = pa.PyAudio()
    try:
        all_ = _wasapi_devices(audio)
    finally:
        audio.terminate()
    microphones = [u["name"] for u in all_ if u["maxInputChannels"] and not u.get("isLoopbackDevice")]
    return microphones, [u["name"].removesuffix(LOOPBACK) for u in all_ if u.get("isLoopbackDevice")]


def pick_devices(audio: pa.PyAudio, microphone: str, output: str) -> tuple[dict[str, dict], list[str]]:
    """What to record: the devices selected by name, and the Windows defaults for empty or missing ones.
    Returns the track devices and notes on what had to be replaced (the selected device is disconnected)."""
    all_, result, notes = _wasapi_devices(audio), {}, []
    loopbacks = {u["name"].removesuffix(LOOPBACK): u for u in all_ if u.get("isLoopbackDevice")}
    if output and output not in loopbacks:
        notes.append(tr('Selected output "{output}" not found, recording the Windows default instead', output=output))
    result["собеседники"] = loopbacks.get(output) or audio.get_default_wasapi_loopback()
    inputs = {u["name"]: u for u in all_ if u["maxInputChannels"] and not u.get("isLoopbackDevice")}
    if microphone and microphone not in inputs:
        notes.append(
            tr(
                'Selected microphone "{microphone}" not found, recording the Windows default instead',
                microphone=microphone,
            )
        )
    by_default = audio.get_host_api_info_by_type(pa.paWASAPI)["defaultInputDevice"]
    if microphone in inputs:
        result["микрофон"] = inputs[microphone]
    elif by_default >= 0:
        result["микрофон"] = audio.get_device_info_by_index(by_default)
    return result, notes


def defaults() -> tuple[str | None, str | None]:
    """The Windows default output and microphone devices (PortAudio does not see them change)."""
    result = []
    for fetch in (AudioUtilities.GetSpeakers, AudioUtilities.GetMicrophone):
        try:
            result.append(fetch().id)
        except (comtypes.COMError, AttributeError):  # no device
            result.append(None)
    return result[0], result[1]


def collect(folder: pathlib.Path) -> str:
    """Joins each track's chunks from the manifest into one 16 kHz FLAC, gaps become silence; deletes the chunks. Then
    the video; the result is what to tell the user if the video was not assembled."""
    manifest = _manifest(folder)
    for name in sorted({k["дорожка"] for k in manifest}):
        chunks = [
            (int(k["начало"] * recognition.SAMPLE_RATE), folder / k["файл"])
            for k in manifest
            if k["дорожка"] == name and (folder / k["файл"]).exists() and (folder / k["файл"]).stat().st_size >= 1024
        ]
        if chunks:
            recognition.join_flac(chunks, folder / f"{name}.flac")
    for k in manifest:
        (folder / k["файл"]).unlink(missing_ok=True)
    (folder / MANIFEST).unlink(missing_ok=True)
    return _assemble_video(folder)


def _assemble_video(folder: pathlib.Path) -> str:
    """Video after the audio tracks are ready; a failure keeps the chunks and does not fail the recording."""
    try:
        screen.assemble(folder)
    except subprocess.CalledProcessError as e:
        log.warning("video %s was not assembled: %s", folder.name, (e.stderr or b"")[-2000:])
        return tr(screen.NOT_ASSEMBLED, reason=tr("the screen recording pieces could not be joined"))
    except OSError as e:
        log.warning("video %s was not assembled: %s", folder.name, e)
        return tr(screen.NOT_ASSEMBLED, reason=failures.text(e).rstrip("."))
    return ""


def interrupted_all(recordings: pathlib.Path, except_: pathlib.Path | None = None) -> list[pathlib.Path]:
    """Recording folders with a manifest of audio or screen chunks: the recording was cut off or the video was not
    assembled."""
    folders = {p.parent for manifest in (MANIFEST, screen.CHUNK_LIST) for p in recordings.glob(f"*/{manifest}")}
    return sorted(p for p in folders if p != except_)


def new_folder(recordings: pathlib.Path) -> pathlib.Path:
    base_name = recordings / datetime.datetime.now().strftime(catalog.FOLDER_NAME)
    folder, number = base_name, 2
    while folder.exists():
        folder, number = base_name.with_name(f"{base_name.name}_{number}"), number + 1
    folder.mkdir(parents=True)
    return folder


@dataclasses.dataclass
class Events:
    """What the recording reports outside; called from the background recording thread."""

    change_item: Callable[[str], None] = lambda _text: None
    ended: Callable[[pathlib.Path, str], None] = lambda _folder, _error: None
    screen_stopped: Callable[[], None] = lambda: None  # screen recording failed, audio keeps recording


class Recording:
    """Recording into a folder: all audio work runs in one background thread, levels and events go outside."""

    def __init__(
        self,
        folder: pathlib.Path,
        events: Events | None = None,
        devices: tuple[str, str] = ("", ""),
        screen_capture: screen.Capture | None = None,
    ):
        """devices is (microphone, output) by name from settings; empty means as selected in Windows. screen_capture is
        what to capture along with the audio (a window or monitor and the quality); None means audio only."""
        self.folder = folder
        self._screen = screen_capture and screen.ScreenRecorder(
            folder, screen_capture, self.duration, self._screen_failed
        )
        self.events = events or Events()
        self.microphone, self.output = devices
        self.started_at = 0.0
        self._paused_total = 0.0
        self._paused_at: float | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._tracks: dict[str, Track] = {}
        self._names: dict[str, str] = {}
        self._audio: pa.PyAudio | None = None
        self._number = 0
        self._worker = threading.Thread(target=self._work, name="запись", daemon=True)

    @property
    def records_screen(self) -> bool:
        return bool(self._screen) and not self._screen.error

    def _screen_failed(self, text: str) -> None:
        self.events.change_item(text)
        self.events.screen_stopped()

    def start_now(self) -> None:
        self.started_at = time.monotonic()
        self._worker.start()

    def stop(self) -> None:
        """Asks to stop; the end arrives as an event after the tracks are assembled."""
        self._stop.set()

    def wait(self, seconds: float | None = None) -> None:
        self._worker.join(seconds)

    def duration(self) -> float:
        """How much is recorded, without pauses: recording chunks and marks are placed by this clock."""
        if not self.started_at:
            return 0.0
        now = self._paused_at or time.monotonic()
        return now - self.started_at - self._paused_total

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    def pause(self, on: bool) -> None:
        """Pauses the recording: the break gets into neither the audio nor the transcript."""
        if on == self.paused:
            return
        now = time.monotonic()
        with self._lock:
            for d in self._tracks.values():
                d.pause(on, now)
            if on:
                self._paused_at = now
            else:
                self._paused_total += now - (self._paused_at or now)
                self._paused_at = None
        if self._screen:
            self._screen.pause(on)

    def mark(self) -> float:
        """Puts a "★ Mark" at this point of the recording; returns its time."""
        seconds = self.duration()
        transcript.mark(self.folder, seconds)
        return seconds

    def levels(self) -> dict[str, int]:
        """Track peaks since the previous call (0-32767)."""
        with self._lock:
            return {name: d.clear_peak() for name, d in self._tracks.items()}

    def devices(self) -> dict[str, str]:
        """Names of the devices being recorded now."""
        with self._lock:
            return dict(self._names)

    def _new_tracks(self, audio: pa.PyAudio, devices: dict[str, dict], tracks: dict[str, Track]) -> None:
        """Opens a track per device into tracks and adds them to the manifest."""
        now, manifest = time.monotonic(), _manifest(self.folder)
        start = self.duration()
        for name, device in devices.items():
            file = f"{name}_{self._number:02d}.wav"
            tracks[name] = Track(audio, device, self.folder / file, now, fill=name == "собеседники")
            if self.paused:
                tracks[name].pause(True, now)
            manifest.append({"дорожка": name, "файл": file, "начало": start, "устройство": device["name"]})
        transcript.write_json(self.folder / MANIFEST, manifest)

    def _open_item(self) -> None:
        """Opens the tracks of the next chunk. If a device does not open, the tracks already opened are closed and
        their files deleted: a stream left running would keep writing after the recording ended."""
        self._number += 1
        audio = pa.PyAudio()  # PortAudio sees only the devices present at startup
        tracks: dict[str, Track] = {}
        try:
            devices, notes = pick_devices(audio, self.microphone, self.output)
            self._new_tracks(audio, devices, tracks)
        except BaseException:
            _close_all(list(tracks.values()), audio)
            for d in tracks.values():
                d.path.unlink(missing_ok=True)
            raise
        with self._lock:  # a pause or resume pressed while the devices opened applies to the new tracks too
            for d in tracks.values():
                d.pause(self.paused, max(self._paused_at or time.monotonic(), d.started_at))
            self._audio, self._tracks = audio, tracks
            self._names = {name: u["name"] for name, u in devices.items()}
        for note in notes:
            self.events.change_item(note)
        if "микрофон" not in tracks:
            self.events.change_item(tr("No microphone found, recording computer audio only"))

    def _close_tracks(self) -> None:
        with self._lock:
            tracks, audio, self._tracks, self._audio = self._tracks, self._audio, {}, None
        if failure := _close_all(list(tracks.values()), audio):
            raise failure

    def _watch(self) -> None:
        devices, seconds = defaults(), 0
        while not self._stop.wait(TICK_S):
            seconds += 1
            if seconds % CHECK_TICKS == 0 and (defaults() != devices or not self._alive()):
                self._close_tracks()
                self._open_item()
                devices = defaults()
                self.events.change_item(tr("Recording switched to other devices"))
            with self._lock:
                for d in self._tracks.values():
                    d.catch_up(time.monotonic())
                    if seconds % FLUSH_TICKS == 0:
                        d.reset()

    def _alive(self) -> bool:
        with self._lock:
            return all(d.alive() for d in self._tracks.values())

    def _record(self) -> str:
        """Records until stopped; returns what to tell the user if the recording broke off."""
        comtypes.CoInitialize()
        error = ""
        try:
            self._open_item()
            if self._screen:
                self._screen.start()
            self._watch()
        except LookupError:  # PyAudioWPatch: Windows has no output device, other participants cannot be recorded
            log.exception("no output device")
            error = tr(
                "No headphones or speakers found, so other participants can't be recorded. Connect them and start "
                "recording again"
            )
        except Exception as e:  # noqa: BLE001 - top of the recording thread: any failure must end it with a message
            log.exception("recording stopped unexpectedly")
            error = tr("Recording stopped unexpectedly. {reason}", reason=failures.text(e))
        closing = self._release()
        return error or closing

    def _release(self) -> str:
        """Stops the screen and closes the tracks: each step runs even if the one before failed."""
        error = ""
        for step in (self._screen and self._screen.stop, self._close_tracks, comtypes.CoUninitialize):
            try:
                if step:
                    step()
            except Exception as e:  # the chunks on disk are still assembled
                log.exception("closing the recording")
                error = error or tr("Recording stopped unexpectedly. {reason}", reason=failures.text(e))
        return error

    def _work(self) -> None:
        error = ""
        try:
            error = self._record()
            if note := collect(self.folder):
                self.events.change_item(note)
        except Exception as e:  # the end must arrive, or the app waits on "Assembling" forever
            log.exception("assembling the recording")
            error = error or tr("Couldn't assemble the recording: {reason}", reason=failures.text(e))
        finally:
            self.events.ended(self.folder, error)
