# SPDX-License-Identifier: GPL-3.0-only
"""Windows integration: autostart, opening files, hotkeys, the Recycle Bin, bringing windows to the front, hiding
from screen sharing."""

import ctypes
import logging
import os
import pathlib
import subprocess
import sys
import winreg
from ctypes import wintypes

from knotes import providers
from knotes.language import tr, tr_key

AUTOSTART = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "QG Knotes"
# command line flags of versions up to 0.20: shortcuts and scripts made for them keep working
OLD_FLAGS = {
    "--запись": "--record",
    "--настройки": "--settings",
    "--расшифровать": "--transcribe",
    "--заново": "--again",
    "--перезапуск": "--restart",
    "--локально": "--local-engine",
}
WDA_EXCLUDEFROMCAPTURE = 0x11
MODIFIERS = {"ctrl": 0x2, "alt": 0x1, "shift": 0x4, "meta": 0x8}  # MOD_CONTROL, MOD_ALT, MOD_SHIFT, MOD_WIN
HOTKEYS = {
    "space": 0x20,
    "pgup": 0x21,
    "pgdown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "print": 0x2C,
    "ins": 0x2D,
    "del": 0x2E,
    "pause": 0x13,
    "backspace": 0x08,
    "tab": 0x09,
    "return": 0x0D,
    "enter": 0x0D,
    "esc": 0x1B,
}
FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
FOF_WANTNUKEWARNING = 0x4000  # no Recycle Bin: ask instead of deleting permanently without a word
FFMPEG_MISSING = tr_key("ffmpeg не найден: переустановите программу или проверьте карантин антивируса.")
log = logging.getLogger(__name__)


class FfmpegMissing(providers.ProviderError):
    """The ffmpeg shipped with the program is gone; the text can be shown to the user."""


def ffmpeg() -> str:
    """Path to the ffmpeg shipped with the program (the imageio-ffmpeg build)."""
    import imageio_ffmpeg  # not every caller needs it loaded

    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except RuntimeError as error:
        log.exception("ffmpeg not found")
        raise FfmpegMissing(tr(FFMPEG_MISSING)) from error


def english_flags(arguments: list[str]) -> list[str]:
    return [OLD_FLAGS.get(argument, argument) for argument in arguments]


def startup(*arguments: str, console: bool = False) -> tuple[list[str], dict[str, str]]:
    """How to start the program with arguments: the built exe, or Python with the package during development (then
    also the package path for PYTHONPATH). console - python.exe instead of pythonw.exe."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *arguments], {}
    python = pathlib.Path(sys.executable).with_name("python.exe" if console else "pythonw.exe")
    return [str(python), "-m", "knotes", *arguments], {"PYTHONPATH": str(pathlib.Path(__file__).parents[1])}


def launch_copy(*arguments: str) -> None:
    """A new copy of the program as a separate process: it outlives this one."""
    command, extra_env = startup(*arguments)
    subprocess.Popen(
        command,
        env=os.environ | extra_env,
        close_fds=True,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )


def launch_command() -> str:
    """Windows autostart line: the program path in quotes and the arguments."""
    program, *arguments = startup("--autostart")[0]
    return " ".join([f'"{program}"', *arguments])


def autostart_enabled(name: str = NAME) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART) as key:
            winreg.QueryValueEx(key, name)
    except OSError:  # no such value
        return False
    return True


def set_autostart(enable: bool, name: str = NAME) -> None:
    """A fresh Windows profile may have no Run key: turning off then does nothing, turning on creates the key."""
    if not enable and not autostart_enabled(name):
        return
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, AUTOSTART, 0, winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, launch_command())
        else:
            winreg.DeleteValue(key, name)


def windows_shortcut(text: str) -> tuple[int, int] | None:
    """Qt's "Ctrl+Alt+R" -> (modifiers, key code) for RegisterHotKey; None - not usable.

    Without a modifier only F1-F24 are usable: otherwise the key would stop typing in other programs.
    """
    parts = [ch.strip().lower() for ch in text.split("+") if ch.strip()]
    if not parts or any(ch not in MODIFIERS for ch in parts[:-1]):
        return None
    modifiers = sum(MODIFIERS[ch] for ch in parts[:-1])
    key_seq = parts[-1]
    if len(key_seq) == 1 and key_seq.isascii() and key_seq.isalnum():
        code = ord(key_seq.upper())
    elif key_seq[0] == "f" and key_seq[1:].isdigit() and 1 <= int(key_seq[1:]) <= 24:
        return modifiers, 0x6F + int(key_seq[1:])
    else:
        code = HOTKEYS.get(key_seq)
    return (modifiers, code) if code and modifiers else None


def open_view(path: pathlib.Path) -> None:
    """A file in the default program, a folder in Explorer."""
    os.startfile(path)  # noqa: S606 - the path is from our own recordings folder


def open_text(path: pathlib.Path, in_notepad: bool) -> None:
    """A text file in Notepad or in the program Windows assigned to such files."""
    if in_notepad:  # full path: a notepad.exe in the current or the program folder cannot replace Notepad
        subprocess.Popen(
            [str(pathlib.Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "notepad.exe"), str(path)]
        )
    else:
        open_view(path)


class _FileAction(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


def to_recycle_bin(path: pathlib.Path) -> bool:
    """A folder or file to the Windows Recycle Bin, where it can be restored; False - failed. If there is no Recycle
    Bin (a network or removable drive, the bin is off or full), Windows asks whether to delete permanently."""
    action = _FileAction(
        None,
        FO_DELETE,
        str(path) + "\0",
        None,
        FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING,
        0,
        None,
        None,
    )
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(action))
    return result == 0 and not action.fAnyOperationsAborted


def bring_to_front(window: object) -> None:
    """Show the window above the others and give it focus.

    The program lives by the clock without its own window, and Windows does not let such a program bring a window
    forward: it opens behind the others. A short "always on top" and back raises it. A minimized window comes back
    as it was before, a maximized one stays maximized.
    """
    from PySide6.QtCore import Qt  # the engine process imports this module without Qt

    window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
    window.show()
    hwnd = int(window.winId())
    for on_top in (-1, -2):  # HWND_TOPMOST, then HWND_NOTOPMOST
        ctypes.windll.user32.SetWindowPos(hwnd, on_top, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)  # keep size and place
    window.raise_()
    window.activateWindow()
    ctypes.windll.user32.SetForegroundWindow(hwnd)


def window_ahead() -> int:
    """The window in use now: after the notepad the focus returns there, to the meeting program."""
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def restore_window(window: int) -> None:
    if window:
        ctypes.windll.user32.SetForegroundWindow(window)


def custom_exe() -> str:
    return sys.executable


def client_animations_enabled() -> bool:
    """Windows Accessibility > Visual effects > Animation effects."""
    enabled = wintypes.BOOL()
    return bool(ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0) and enabled.value)


def process_path(pid: int) -> str:
    """Full path of the process program; empty if Windows does not give it (no such process, another user's)."""
    process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return ""
    try:
        path, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size))
        return path.value if ok else ""
    finally:
        ctypes.windll.kernel32.CloseHandle(process)


def _pids() -> list[int]:
    pids, size = (wintypes.DWORD * 4096)(), wintypes.DWORD()
    if not ctypes.windll.kernel32.K32EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(size)):
        return []
    return list(pids[: size.value // ctypes.sizeof(wintypes.DWORD)])


def processes_in(folder: pathlib.Path) -> list[int]:
    """Processes started from programs inside the folder: other copies, "QG Knotes.exe --mcp" run by Claude
    Desktop."""
    where = pathlib.PureWindowsPath(folder)
    return [
        pid
        for pid in _pids()
        if pid != os.getpid() and (path := process_path(pid)) and pathlib.PureWindowsPath(path).is_relative_to(where)
    ]


def hide_from_capture(window: int, hide_: bool) -> None:
    """Keeps the window out of screen sharing and recordings (Windows 10 2004+)."""
    ctypes.windll.user32.SetWindowDisplayAffinity(window, WDA_EXCLUDEFROMCAPTURE if hide_ else 0)
