# SPDX-License-Identifier: GPL-3.0-only
"""Meeting detector: Windows records in the registry which program is using the microphone right now.

The ConsentStore\\microphone key: subkeys of packaged apps (MSTeams_8wekyb3d8bbwe) and NonPackaged
with exe paths that use "#" instead of "\\". A program is using the microphone if LastUsedTimeStart is set and
LastUsedTimeStop = 0. Recording is offered only if the microphone is busy for more than a few seconds (not an
accidental click), once per meeting and not right after a recording ends. Muting releases the microphone briefly:
the meeting does not end because of it, and a declined recording stays declined until the meeting ends.
"""

import dataclasses
import pathlib
import winreg

KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
LONGER_S = 3
POLL_S = 1
BREAK_S = 60
END_S = 20  # released the microphone longer than this: the meeting has likely ended (muting releases it briefly)
NEW_S = 300  # released longer than this: next time is a new meeting, and recording can be offered again


def _connect_all(key: winreg.HKEYType) -> list[str]:
    names, number = [], 0
    while True:
        try:
            names.append(winreg.EnumKey(key, number))
        except OSError:  # no more subkeys
            return names
        number += 1


def _busy(key: winreg.HKEYType) -> bool:
    try:
        started_at = winreg.QueryValueEx(key, "LastUsedTimeStart")[0]
        stop_ = winreg.QueryValueEx(key, "LastUsedTimeStop")[0]
    except OSError:  # the program has not used the microphone yet
        return False
    return bool(started_at) and stop_ == 0


def _read(path: str) -> dict[str, bool]:
    result = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as root:
            for name in _connect_all(root):
                if name == "NonPackaged":
                    exe = _read(f"{path}\\{name}")
                    result |= {exe_path.replace("#", "\\"): busy for exe_path, busy in exe.items()}
                    continue
                with winreg.OpenKey(root, name) as key:
                    result[name] = _busy(key)
    except OSError:  # no key (old Windows or microphone access denied)
        return result
    return result


def read() -> dict[str, bool]:
    """Programs Windows gave the microphone to: exe path or package name -> whether it is busy now."""
    return _read(KEY)


def meetings(status: dict[str, bool], apps: list[str], custom_one: str = "") -> set[str]:
    """Which meeting apps are using the microphone now; our own program does not count."""
    needed_models = {p.lower() for p in apps}
    result = set()
    for name, busy in status.items():
        short_text = pathlib.PureWindowsPath(name).name
        if busy and short_text.lower() in needed_models and name.lower() != custom_one.lower():
            result.add(short_text)
    return result


@dataclasses.dataclass
class Step:
    suggest: set[str]
    finished_items: set[str]


class Detector:
    """Decides, poll step by poll step, when to offer recording and when a meeting has ended."""

    def __init__(self, apps: list[str], custom_one: str = ""):
        self.apps, self.custom_one = apps, custom_one
        self._s: dict[str, float] = {}  # app -> when its meeting started
        self._released: dict[str, float] = {}  # app -> since when it has not held the microphone
        self._finished: set[str] = set()  # the meeting end was already reported
        self._offered: set[str] = set()  # recording was already offered in this meeting: not a second time
        self._silence_until = 0.0

    def recording_ended(self, now: float) -> None:
        """Do not offer for a while after a recording: the meeting may not have released the microphone yet."""
        self._silence_until = now + BREAK_S
        self._offered |= set(self._s)

    def _meetings_running(self, busy_all: set[str], now: float) -> set[str]:
        """Updates which apps have a meeting going; returns those whose meeting ended at this step. A short release
        of the microphone (muted) does not end a meeting; after a long break the same program means a new meeting."""
        for p in busy_all:
            released = self._released.pop(p, None)
            if p not in self._s or (released is not None and now - released >= NEW_S):
                self._s[p] = now
                self._offered.discard(p)
                self._finished.discard(p)
        for p in set(self._s) - busy_all:
            self._released.setdefault(p, now)
        finished_items = {p for p, s in self._released.items() if now - s >= END_S} - self._finished
        self._finished |= finished_items
        return finished_items

    def step(self, status: dict[str, bool], now: float, recording_now: bool) -> Step:
        busy_all = meetings(status, self.apps, self.custom_one)
        finished_items = self._meetings_running(busy_all, now)
        if recording_now or now < self._silence_until:
            return Step(set(), finished_items)
        suggest = {p for p in busy_all if now - self._s[p] >= LONGER_S} - self._offered
        self._offered |= suggest
        return Step(suggest, finished_items)
