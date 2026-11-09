# SPDX-License-Identifier: GPL-3.0-only
"""Meeting transcript: phrases, participant names and marks in the meeting folder; расшифровка.md is built from them.

Speech recognition writes the phrases (фразы.json); names (имена.json: "Собеседник 1" -> "Анна") and marks
(метки.json: "★ important" moments during recording) can be changed any number of times: расшифровка.md
is rebuilt without sending the audio to the provider again.
"""

import collections
import contextlib
import copy
import ctypes
import errno
import functools
import json
import logging
import os
import pathlib
import re
import sys
import tempfile
import threading
import time as clock
from collections.abc import Callable, Iterator
from typing import Any, Concatenate

from knotes.language import tr, tr_key

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

PHRASES, NAMES, MARKS, TRANSCRIPT = "фразы.json", "имена.json", "метки.json", "расшифровка.md"
MANIFEST = "запись.json"  # chunks of an unfinished recording
TRACKS = ("микрофон", "собеседники", "общая")  # <name>.flac
ANSWERS = "scribe.json"  # raw recognition answers
SOURCE = "источник.txt"  # path of the ready-made file the audio was taken from
MARKER = tr_key("★ Отметка")  # stored in Russian, shown on screen via tr()
NOTE = "✎"  # analyst's note: the icon in the transcript and on a minutes item that grew out of a note
ME = "Я"  # the owner, the speaker of the microphone track; settings.my_name replaces it in the minutes
TIME = r"(\d{1,2}):(\d{2})(?::(\d{2}))?"  # mm:ss or h:mm:ss
BROKEN = ".испорчен"  # a corrupted file is set aside next to it under its name with this before the suffix
REPLACE_TRIES = 8  # on Windows another process (the window, the MCP server, an antivirus) may hold the file a moment
LOCK = ".замок"  # lock file of a folder whose files are edited; empty, hidden on Windows, never deleted
LOCK_WAIT_S = 5.0  # an edit waits this long for another one in the same folder, then gives up with Busy
_BUSY = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}  # the lock is held: msvcrt gives EACCES, flock EWOULDBLOCK
log = logging.getLogger(__name__)
_held = threading.local()  # folders whose lock this thread holds: an edit inside an edit does not wait for itself


class Busy(TimeoutError):
    """Another edit kept the folder locked too long: this edit is not made, the other one is not lost. The text is for
    the user."""


def time(seconds_list: float) -> str:
    ch, rest = divmod(int(seconds_list), 3600)
    return f"{ch}:{rest // 60:02d}:{rest % 60:02d}" if ch else f"{rest // 60:02d}:{rest % 60:02d}"


def seconds_list(label: re.Match) -> int:
    """[05:40] -> 340, [1:09:10] -> 4150."""
    numbers_ = [int(ch) for ch in label.groups() if ch is not None]
    return numbers_[0] * 3600 + numbers_[1] * 60 + numbers_[2] if len(numbers_) == 3 else numbers_[0] * 60 + numbers_[1]


def read_json(path: pathlib.Path, by_default: object) -> object:
    """JSON file; if the file is missing, corrupted (the write was cut off, it was edited by hand) or holds the wrong
    type (a list instead of a dict), returns by_default without failing. A corrupted file is set aside first: the
    next write replaces it."""
    if not path.exists():
        return by_default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        log.warning("corrupted file %s, reading it as empty; a copy is %s", path, set_aside(path))
        return by_default
    if by_default is not None and not isinstance(data, type(by_default)):
        log.warning("file %s does not hold what was expected, reading it as empty; a copy is %s", path, set_aside(path))
        return by_default
    return data


def set_aside(path: pathlib.Path, copy_: pathlib.Path | None = None) -> pathlib.Path | None:
    """A copy of a corrupted file next to it, by default "задачи.испорчен.json"; None if it cannot be made."""
    copy_ = copy_ or path.with_name(f"{path.stem}{BROKEN}{path.suffix}")
    try:
        content = path.read_bytes()
        if not copy_.exists() or copy_.read_bytes() != content:
            copy_.write_bytes(content)
    except OSError as error:
        log.warning("cannot set aside %s: %s", path, error)
        return None
    return copy_


def write_text(path: pathlib.Path, text: str) -> None:
    """Through a temporary file next to it and os.replace: a cut-off write leaves the old file whole, and the window
    and the MCP server writing the same file never mix their writes."""
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    temporary = pathlib.Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
        replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def replace(source: pathlib.Path, target: pathlib.Path, tries: int = REPLACE_TRIES, longest_s: float = 1.0) -> None:
    """os.replace with retries: a file another process (the window, the MCP server, an antivirus) has open cannot be
    replaced on Windows for a moment."""
    for attempt in range(tries):
        try:
            os.replace(source, target)
        except PermissionError:
            if attempt + 1 == tries:
                log.warning("file %s stays held by another program, not written", target)
                raise
            clock.sleep(min(0.05 * 2**attempt, longest_s))
        else:
            return


def write_json(path: pathlib.Path, data: object, indent: int | None = 1) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=indent))


def update_json(path: pathlib.Path, by_default: object, change: Callable[[Any], object]) -> object:
    """Read-change-write of a JSON file under the lock of its folder: change gets the data as read_json gives it
    and returns the new data, written only if it differs. The window, the MCP server and their threads wait for each
    other's edit instead of writing over it with what they read before it. Returns the new data."""
    with locked(path):
        data = read_json(path, by_default)
        new = change(copy.deepcopy(data))
        if new != data:
            write_json(path, new)
        return new


def locked_edit[**P, R](
    relative: pathlib.Path,
) -> Callable[[Callable[Concatenate[pathlib.Path, P], R]], Callable[Concatenate[pathlib.Path, P], R]]:
    """A decorator for a function that takes the recordings folder first and edits <recordings>/relative: from
    reading to writing it holds the lock of that file's folder, created first (the lock does not create folders)."""

    def decorator(function: Callable[Concatenate[pathlib.Path, P], R]) -> Callable[Concatenate[pathlib.Path, P], R]:
        @functools.wraps(function)
        def under_lock(recordings: pathlib.Path, *arguments: P.args, **named: P.kwargs) -> R:
            path = recordings / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with locked(path):
                return function(recordings, *arguments, **named)

        return under_lock

    return decorator


@contextlib.contextmanager
def locked(path: pathlib.Path) -> Iterator[None]:
    """Holds the lock of the file's folder for a whole edit of its files, waiting up to LOCK_WAIT_S for another one;
    reading needs no lock, files are replaced whole. The lock belongs to the open handle of LOCK: the operating system
    drops it when the process dies mid-edit, so it never stays held. Taken again in the same thread it is the same
    lock."""
    folder = os.path.normcase(os.path.abspath(path.parent))
    held = vars(_held).setdefault("folders", set())
    if folder in held:
        yield
        return
    handle = _lock(path.parent / LOCK)
    held.add(folder)
    try:
        yield
    finally:
        held.discard(folder)
        _release(handle)


def _lock(lock: pathlib.Path) -> int | None:
    """Handle holding the lock; None if the file system cannot lock: the edit goes on without it, as before."""
    new = not lock.exists()
    handle = os.open(lock, os.O_RDWR | os.O_CREAT)
    if new:
        _hide(lock)
    deadline, pause = clock.monotonic() + LOCK_WAIT_S, 0.01
    while True:
        try:
            _try_lock(handle)
            return handle
        except OSError as error:
            if error.errno not in _BUSY:
                log.warning("cannot lock %s, editing without the lock: %s", lock, error)
                os.close(handle)
                return None
            if clock.monotonic() >= deadline:
                os.close(handle)
                log.warning("%s stayed locked for %s s, the edit is not made", lock, LOCK_WAIT_S)
                raise Busy(
                    tr(
                        "Files in {folder} are being changed by QG Knotes or your AI assistant right now. Nothing is "
                        "lost: try again in a few seconds.",
                        folder=lock.parent,
                    )
                ) from None
        clock.sleep(pause)
        pause = min(pause * 2, 0.1)


def _release(handle: int | None) -> None:
    if handle is None:
        return
    try:
        _unlock(handle)
    finally:
        os.close(handle)


if sys.platform == "win32":  # a byte of the lock file, by the handle: another handle, of this process too, waits

    def _try_lock(handle: int) -> None:
        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)

    def _unlock(handle: int) -> None:
        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)

    def _hide(lock: pathlib.Path) -> None:
        ctypes.windll.kernel32.SetFileAttributesW(str(lock), 2)  # FILE_ATTRIBUTE_HIDDEN

else:  # flock: by the open file, so threads of one process wait for each other too

    def _try_lock(handle: int) -> None:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: int) -> None:
        fcntl.flock(handle, fcntl.LOCK_UN)

    def _hide(_lock: pathlib.Path) -> None:
        """A name starting with a dot is hidden already."""


def save(folder: pathlib.Path, phrases: list[dict], info: dict, notes: list[str]) -> pathlib.Path:
    """Recognition phrases (start, end, who, text) and info about the recognition; returns the built transcript."""
    with locked(folder / PHRASES):
        write_json(
            folder / PHRASES,
            {
                "сведения": info,
                "заметки": notes,
                "фразы": [{k: f[k] for k in ("начало", "конец", "кто", "текст")} for f in phrases],
            },
        )
        return collect(folder)


def phrases(folder: pathlib.Path) -> dict | None:
    """Saved recognition: {"сведения", "заметки", "фразы"}; None if the meeting is not recognized yet."""
    return read_json(folder / PHRASES, None)


def names(folder: pathlib.Path) -> dict[str, str]:
    return read_json(folder / NAMES, {})


def set_names(folder: pathlib.Path, new_items: dict[str, str]) -> pathlib.Path:
    """Participant names instead of speaker labels, over the saved ones: a name not given stays as it is, an empty
    name restores the label."""

    def merged(saved: dict[str, str]) -> dict[str, str]:
        result = saved | {who: name.strip() for who, name in new_items.items()}
        return {who: name for who, name in result.items() if name and name != who}

    with locked(folder / NAMES):
        update_json(folder / NAMES, {}, merged)
        return collect(folder)


def labels(folder: pathlib.Path) -> list[dict]:
    return read_json(folder / MARKS, [])


def mark(folder: pathlib.Path, seconds: float, text: str = MARKER) -> None:
    """A mark at this moment of the recording; in the transcript it becomes a line among the phrases."""
    with locked(folder / MARKS):
        update_json(folder / MARKS, [], lambda marks: [*marks, {"время": round(seconds, 1), "текст": text}])
        _rebuild(folder)


def delete_label(folder: pathlib.Path, seconds: float, text: str) -> None:
    """Remove a mark; of identical marks at the same second only one is removed."""

    def without_one(marks: list[dict]) -> list[dict]:
        extra_one = next((m for m in marks if m["время"] == round(seconds, 1) and m["текст"] == text), None)
        return [m for m in marks if m is not extra_one]

    with locked(folder / MARKS):
        update_json(folder / MARKS, [], without_one)
        _rebuild(folder)


def _rebuild(folder: pathlib.Path) -> None:
    """If the transcript exists, rebuild it with the new mark; during recording there is none yet."""
    if (folder / PHRASES).exists():
        collect(folder)


def fix_phrase(folder: pathlib.Path, number: int, text: str) -> pathlib.Path:
    """A manual edit of a phrase text; recognizing again overwrites it."""

    def fixed(data: dict | None) -> dict:
        if data is None:
            raise FileNotFoundError(f"Встреча {folder.name} еще не распознана")
        data["фразы"][number]["текст"] = text.strip()
        return data

    with locked(folder / PHRASES):
        update_json(folder / PHRASES, None, fixed)
        return collect(folder)


def participants(folder: pathlib.Path) -> list[str]:
    """Who speaks in the meeting, in order of their first word: "Я", "Собеседник 1"..."""
    data = phrases(folder) or {"фразы": []}
    return list(dict.fromkeys(f["кто"] for f in data["фразы"]))


def shares(folder: pathlib.Path) -> dict[str, float]:
    """Each participant's share of speaking time by the length of their phrases; the shares add up to 1."""
    time: dict[str, float] = collections.defaultdict(float)
    for f in (phrases(folder) or {"фразы": []})["фразы"]:
        time[f["кто"]] += max(0.0, f["конец"] - f["начало"])
    total = sum(time.values())
    return {who: seconds / total for who, seconds in time.items()} if total else {}


def blocks(folder: pathlib.Path, notes: list[tuple[int, str]] | None = None) -> list[dict]:
    """Transcript for display in time order: consecutive phrases of one participant form one block
    {"кто", "фразы": [(number, start, text)]}, a mark or a note with a minute forms a block {"метка", "время"}."""
    data = phrases(folder) or {"фразы": []}
    events = [(f["начало"], 1, number, f) for number, f in enumerate(data["фразы"])]
    events += [(m["время"], 0, -1, m) for m in labels(folder)]
    events += [(second, 0, -1, {"текст": f"{NOTE} {text}"}) for second, text in notes or []]
    result: list[dict] = []
    for seconds, kind, number, recording in sorted(events, key=lambda s: s[:3]):
        if not kind:
            result.append({"метка": recording["текст"], "время": seconds})
        elif result and result[-1].get("кто") == recording["кто"]:
            result[-1]["фразы"].append((number, seconds, recording["текст"]))
        else:
            result.append({"кто": recording["кто"], "фразы": [(number, seconds, recording["текст"])]})
    return result


def to_markdown(title: str, lines: list[tuple[float, str]], info: dict, notes: list[str]) -> str:
    """Transcript in Markdown; lines are (second, "who: text" or a mark) in time order."""
    result = [
        f"# Встреча {title}",
        "",
        f"Длительность {time(info['длительность'])}. Распознано: {info['провайдер']}, "
        f"стоимость {info['стоимость']:.3f} $.",
        "",
    ]
    result += [*notes, *([""] if notes else [])]
    result += [f"[{time(s)}] {text}" for s, text in lines]
    return "\n".join(result) + "\n"


def lines(folder: pathlib.Path) -> list[tuple[float, str]]:
    """(second, "Name: text" or a mark) in time order, with names instead of speaker labels."""
    data = phrases(folder)
    if data is None:
        raise FileNotFoundError(f"Встреча {folder.name} еще не распознана")
    replacement = names(folder)
    result = [(f["начало"], f"{replacement.get(f['кто'], f['кто'])}: {f['текст']}") for f in data["фразы"]]
    result += [(m["время"], m["текст"]) for m in labels(folder)]
    return sorted(result, key=lambda s: s[0])


def collect(folder: pathlib.Path) -> pathlib.Path:
    """Builds расшифровка.md from phrases, names and marks."""
    lines_ = lines(folder)
    data = phrases(folder) or {}
    file = folder / TRANSCRIPT
    write_text(file, to_markdown(folder.name, lines_, data["сведения"], data["заметки"]))
    return file
