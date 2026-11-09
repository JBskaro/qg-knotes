# SPDX-License-Identifier: GPL-3.0-only
"""Screen recording next to the meeting audio: ffmpeg grabs the meeting window area or a monitor in chunks, then
the chunks are joined and muxed with the audio tracks into видео.mp4."""

import ctypes
import dataclasses
import json
import logging
import os
import pathlib
import queue
import re
import subprocess
import tempfile
import threading
from collections.abc import Callable
from ctypes import wintypes

from knotes import failures, system, transcript
from knotes.language import tr_key, tr

VIDEO_FILE = "видео.mp4"  # user data file names stay as they are: existing meeting folders use them
CHUNK_LIST = "экран.json"
CONCAT_LIST = "экран.txt"
AUDIO_TRACKS = ("микрофон.flac", "собеседники.flac", "общая.flac")
FIRST_FRAME_TIMEOUT_S = 10
STOP_TIMEOUT_S = 10
# What to record (settings): the meeting window, the monitor with it, the primary one, or a monitor device name
MEETING_WINDOW, MEETING_SCREEN, PRIMARY = "", "экран_встречи", "основной"
# Quality: frames per second, frame height limit (0 - as captured), hardware bitrate, software CRF
QUALITY = {"эконом": (8, 720, "600k", 32), "обычное": (10, 1080, "1000k", 30), "высокое": (15, 0, "2500k", 26)}
DEFAULT_QUALITY = "обычное"
# GDI grabs a fixed screen area at a constant frame rate. Windows Graphics Capture (ffmpeg 8.1+ gfxcapture) was
# tried for windows: it sends frames only when the picture changes, so a still slide stalls ffmpeg and it ignores
# "q". Hardware encoder first (about 3% CPU on a 1080p screen), the software one if the driver has none
ENCODERS = ("h264_mf", "libx264")
CHUNK_NAME = re.compile(r"экран_\d{2,}\.mkv")
NOT_RECORDING = tr_key("Экран не записывается: {reason}. Звук пишется как обычно.")
NOT_ASSEMBLED = tr_key(
    "Видео встречи не собралось: {reason}. Звук и расшифровка в порядке, куски видео остались в "
    "папке встречи: программа соберет их при следующем запуске."
)
log = logging.getLogger(__name__)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)


@dataclasses.dataclass(frozen=True)
class Monitor:
    device: str  # \\.\DISPLAY1
    x: int
    y: int
    width: int
    height: int
    primary: bool = False
    handle: int = 0  # HMONITOR


@dataclasses.dataclass(frozen=True)
class Capture:
    """What to record: an area of the screen (x, y, width, height) - the meeting window - or, when empty, the whole
    monitor; and the quality preset."""

    monitor: Monitor
    area: tuple[int, int, int, int] = ()
    quality: str = DEFAULT_QUALITY


class _MonitorInfo(ctypes.Structure):  # MONITORINFOEXW
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


def _monitor(handle: int) -> Monitor:
    info = _MonitorInfo()
    info.cbSize = ctypes.sizeof(info)
    ctypes.windll.user32.GetMonitorInfoW(wintypes.HMONITOR(handle), ctypes.byref(info))
    r = info.rcMonitor
    return Monitor(
        info.szDevice, r.left, r.top, r.right - r.left, r.bottom - r.top, bool(info.dwFlags & 1), int(handle or 0)
    )


def monitors() -> list[Monitor]:
    """Monitors in physical pixels, as ffmpeg sees them (the program is DPI-aware)."""
    found: list[Monitor] = []
    callback = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
    )(lambda handle, *_: found.append(_monitor(handle)) or True)
    ctypes.windll.user32.EnumDisplayMonitors(None, None, callback, 0)
    return found


def _exe_name(window: int) -> str:
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
    return pathlib.PureWindowsPath(system.process_path(pid.value)).name


def _window_area(window: int, monitor: Monitor) -> tuple[int, int, int, int]:
    """Visible window bounds (DWM, without the invisible resize border), clipped to its monitor, even-sized."""
    rect = wintypes.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(window, 9, ctypes.byref(rect), ctypes.sizeof(rect)):  # frame bounds
        ctypes.windll.user32.GetWindowRect(window, ctypes.byref(rect))
    left, top = max(rect.left, monitor.x), max(rect.top, monitor.y)
    right, bottom = min(rect.right, monitor.x + monitor.width), min(rect.bottom, monitor.y + monitor.height)
    return left, top, max(2, (right - left) // 2 * 2), max(2, (bottom - top) // 2 * 2)


def meeting_window(programs: list[str]) -> tuple[tuple[int, int, int, int], Monitor] | None:
    """Area and monitor of the largest visible window of the meeting programs; None if there is none."""
    names, best = {p.lower() for p in programs}, [0, 0]  # area, window
    user32 = ctypes.windll.user32

    def visit(window: int, _: int) -> bool:
        rect = wintypes.RECT()
        if user32.IsWindowVisible(window) and not user32.IsIconic(window) and _exe_name(window).lower() in names:
            user32.GetWindowRect(window, ctypes.byref(rect))
            area = (rect.right - rect.left) * (rect.bottom - rect.top)
            if area > best[0]:
                best[:] = [area, window]
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(visit), 0)
    if not best[1]:
        return None
    monitor = _monitor(user32.MonitorFromWindow(best[1], 2))  # MONITOR_DEFAULTTONEAREST
    return _window_area(best[1], monitor), monitor


def choose_capture(
    choice: str,
    programs: list[str],
    quality: str = DEFAULT_QUALITY,
    available: list[Monitor] | None = None,
    find_meeting: Callable[[list[str]], tuple[tuple, Monitor] | None] = meeting_window,
) -> Capture | None:
    """What to record by the settings choice. A missing monitor falls back to the primary one; a meeting window
    that is not found gives None: recording the whole screen instead would show what the user chose to keep out."""
    quality = quality if quality in QUALITY else DEFAULT_QUALITY
    if choice in (MEETING_WINDOW, MEETING_SCREEN):
        found = find_meeting(programs)
        if found is None:
            return None
        area, monitor = found
        return Capture(monitor, area if choice == MEETING_WINDOW else (), quality)
    available = available if available is not None else monitors()
    primary = next((m for m in available if m.primary), available[0])
    return Capture(next((m for m in available if m.device == choice), primary), (), quality)


def capture_command(capture: Capture, file_name: str, encoder: str) -> list[str]:
    """ffmpeg grabbing the area into a Matroska chunk (readable after a crash); progress goes to stdout, "q" on stdin
    ends the chunk cleanly."""
    fps, height, bitrate, crf = QUALITY[capture.quality]
    m = capture.monitor
    x, y, width, height_ = capture.area or (m.x, m.y, m.width, m.height)
    filters = [
        *([f"scale=-2:'min(ih,{height})'"] if height else []),
        "format=nv12" if encoder == "h264_mf" else "format=yuv420p",
    ]
    codec = (
        ["-c:v", "h264_mf", "-b:v", bitrate]
        if encoder == "h264_mf"
        else ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf)]
    )
    return [
        system.ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        "-f",
        "gdigrab",
        "-framerate",
        str(fps),
        "-draw_mouse",
        "1",
        "-offset_x",
        str(x),
        "-offset_y",
        str(y),
        "-video_size",
        f"{width}x{height_}",
        "-i",
        "desktop",
        "-vf",
        ",".join(filters),
        *codec,
        "-y",
        file_name,
    ]


def read_chunks(folder: pathlib.Path) -> list[dict]:
    """Recorded chunks: only this folder's own chunk names, so a broken list cannot point elsewhere."""
    try:
        data = json.loads((folder / CHUNK_LIST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [
        c
        for c in data
        if isinstance(c, dict)
        and CHUNK_NAME.fullmatch(str(c.get("файл", "")))
        and isinstance(c.get("начало"), int | float)
    ]


class _Chunk:
    """One ffmpeg process. The reader thread drains progress (a full pipe would stall ffmpeg) and keeps the frame
    count: once the chunk is closed it gives the exact length, the first report only a rough start (the hardware
    encoder holds back a few frames)."""

    def __init__(self, command: list[str], folder: pathlib.Path):
        self.stderr = tempfile.TemporaryFile()  # noqa: SIM115 - closed in finish()
        self.process = subprocess.Popen(
            command,
            cwd=folder,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            creationflags=_NO_WINDOW,
        )
        self.started = threading.Event()
        self.frames_at_start = self.frames = 0
        self._reader = threading.Thread(target=self._read_progress, daemon=True)
        self._reader.start()

    def _read_progress(self) -> None:
        for line in self.process.stdout:
            if line.startswith(b"frame=") and (frames := int(line[6:].strip() or 0)):
                self.frames = frames
                if not self.started.is_set():
                    self.frames_at_start = frames
                    self.started.set()

    def finish(self) -> str:
        """Stop the chunk cleanly (kill if ffmpeg hangs); returns the tail of ffmpeg's stderr."""
        if self.process.poll() is None:
            try:
                self.process.stdin.write(b"q")
                self.process.stdin.flush()
            except OSError:
                log.debug("ffmpeg already closed its input")
            try:
                self.process.wait(STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self._reader.join(2)  # the final report carries the full frame count
        self.stderr.seek(0)
        tail = self.stderr.read()[-2000:].decode("utf-8", "replace").strip()
        self.stderr.close()
        return tail


class ScreenRecorder:
    """Records a window area or monitor in chunks on its own thread: pause closes the chunk, resume opens the next.
    Chunk starts are taken on the audio recording clock, so the video lines up with the sound. A failure ends only
    the video: on_error(text) tells the user, the audio goes on."""

    def __init__(
        self,
        folder: pathlib.Path,
        capture: Capture,
        clock: Callable[[], float],
        on_error: Callable[[str], None] = lambda _text: None,
    ):
        self.folder, self.capture, self.clock, self.on_error = folder, capture, clock, on_error
        self.error = ""
        self._methods = list(ENCODERS)
        self._fps, self._number = QUALITY[capture.quality][0], 0
        self._chunk: _Chunk | None = None
        self._commands: queue.Queue[str] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="screen", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._commands.put("open")

    def pause(self, on: bool) -> None:
        self._commands.put("close" if on else "open")

    def stop(self) -> None:
        self._commands.put("stop")
        if self._thread.is_alive():
            self._thread.join()

    def _run(self) -> None:
        while (command := self._commands.get()) != "stop":
            if command == "close":
                self._close()
            else:
                self._open()
        self._close()

    def _open(self) -> None:
        while self._chunk is None and not self.error and self._methods:
            self._number += 1
            file_name = f"экран_{self._number:02d}.mkv"
            try:
                chunk = _Chunk(capture_command(self.capture, file_name, self._methods[0]), self.folder)
            except (OSError, system.FfmpegMissing) as error:  # no ffmpeg: the video stops, the audio goes on
                log.warning("screen capture did not start: %s", error)
                self.error = tr(NOT_RECORDING, reason=failures.text(error).rstrip("."))
                self.on_error(self.error)
                return
            if chunk.started.wait(FIRST_FRAME_TIMEOUT_S) and chunk.process.poll() is None:
                self._chunk = chunk
                self._add_chunk(file_name, self.clock() - chunk.frames_at_start / self._fps)
                return
            log.warning("screen capture via %s failed: %s", self._methods.pop(0), chunk.finish())
            (self.folder / file_name).unlink(missing_ok=True)
        if self._chunk is None and not self.error:
            self.error = tr(NOT_RECORDING, reason=tr("no screen capture method worked"))
            self.on_error(self.error)

    def _close(self) -> None:
        """Close the chunk; its start becomes the stop moment minus its length, exact to a frame."""
        if self._chunk is not None:
            stopped = self.clock()
            if tail := self._chunk.finish():
                log.info("ffmpeg while recording the screen: %s", tail)
            if self._chunk.frames:
                self._set_last_start(stopped - self._chunk.frames / self._fps)
            self._chunk = None

    def _add_chunk(self, file_name: str, start: float) -> None:
        self._write_chunks([*read_chunks(self.folder), {"файл": file_name, "начало": round(max(start, 0.0), 3)}])

    def _set_last_start(self, start: float) -> None:
        chunks = read_chunks(self.folder)
        if chunks:
            chunks[-1]["начало"] = round(max(start, 0.0), 3)
            self._write_chunks(chunks)

    def _write_chunks(self, chunks: list[dict]) -> None:
        transcript.write_json(self.folder / CHUNK_LIST, chunks)


def concat_list(chunks: list[dict]) -> str:
    """concat demuxer list: each chunk lasts until the next one starts, so pause gaps do not shift later chunks."""
    lines = []
    for number, chunk in enumerate(chunks):
        lines.append(f"file '{chunk['файл']}'")
        if number + 1 < len(chunks):
            lines.append(f"duration {chunks[number + 1]['начало'] - chunk['начало']:.3f}")
    return "\n".join(lines) + "\n"


def assemble_command(chunks: list[dict], audio: list[str], list_file: str, output: str) -> list[str]:
    """ffmpeg joining the chunks (video copied, not re-encoded) and mixing the audio tracks into AAC."""
    mix: list[str] = []
    if len(audio) > 1:
        inputs = "".join(f"[{number + 1}:a]" for number in range(len(audio)))
        mix = ["-filter_complex", f"{inputs}amix=inputs={len(audio)}:duration=longest:normalize=0[a]", "-map", "[a]"]
    elif audio:
        mix = ["-map", "1:a"]
    return [
        system.ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-itsoffset",
        f"{chunks[0]['начало']:.3f}",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_file,
        *(arg for file_name in audio for arg in ("-i", file_name)),
        "-map",
        "0:v",
        *mix,
        "-c:v",
        "copy",
        *(["-c:a", "aac", "-b:a", "96k"] if audio else []),
        "-movflags",
        "+faststart",
        output,
    ]


def assemble(folder: pathlib.Path) -> None:
    """Join the chunks with the audio tracks into видео.mp4 and remove the chunks. On failure raises OSError or
    CalledProcessError and keeps the chunks."""
    chunks = [c for c in read_chunks(folder) if (folder / c["файл"]).is_file() and (folder / c["файл"]).stat().st_size]
    if chunks:
        audio = [name for name in AUDIO_TRACKS if (folder / name).is_file()]
        (folder / CONCAT_LIST).write_text(concat_list(chunks), encoding="utf-8")
        partial = "видео.part.mp4"
        subprocess.run(
            assemble_command(chunks, audio, CONCAT_LIST, partial),
            cwd=folder,
            capture_output=True,
            check=True,
            creationflags=_NO_WINDOW,
        )
        os.replace(folder / partial, folder / VIDEO_FILE)
    for chunk in read_chunks(folder):
        (folder / chunk["файл"]).unlink(missing_ok=True)
    for name in (CHUNK_LIST, CONCAT_LIST):
        (folder / name).unlink(missing_ok=True)
