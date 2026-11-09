# SPDX-License-Identifier: GPL-3.0-only
"""Meeting notes: the analyst's notepad, as in Granola and Fellow.

Written during the recording (a line keeps the minute it was started at) and after the meeting; the model builds the
minutes around them. On top is the agenda: questions and topics prepared for the meeting. Items marked [x] were
discussed, the rest move to the project's next meeting. The file заметки.md in the meeting folder is plain Markdown,
a line with a minute starts with "[mm:ss] ".
"""

import dataclasses
import pathlib
import re
from collections.abc import Callable

from knotes import catalog, projects, transcript
from knotes.language import tr

FILE = "заметки.md"
AGENDA = "## Повестка"
NOTES = "## Заметки"
ITEM = re.compile(r"^- \[([ xX])\] (.*)$")
MINUTE = re.compile(rf"^\[{transcript.TIME}\] ")
KINDS = ("Решение", "Задача", "Вопрос")  # a line starting "Решение - ..." tells the model which section it goes to


@dataclasses.dataclass(frozen=True)
class NoteLine:
    text: str
    second: int | None = None  # recording minute when the line was started; none after the meeting


@dataclasses.dataclass(frozen=True)
class AgendaItem:
    text: str
    discussed: bool = False


@dataclasses.dataclass(frozen=True)
class Notes:
    agenda: tuple[AgendaItem, ...] = ()
    lines: tuple[NoteLine, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.agenda and not any(s.text.strip() for s in self.lines)


def question(text: str) -> bool:
    """The line is a question: "Вопрос - ..." (also in the UI language), "? ..." or ends with a question mark."""
    text = text.strip().removeprefix("- ")
    return text.startswith(("Вопрос", tr("Question"), "?")) or text.endswith("?")


def from_text(text: str) -> Notes:
    """Sections only at the start of the file: AGENDA as the first line, up to the first NOTES; everything after is
    note lines, even if the analyst typed such a heading."""
    file_lines = text.splitlines()
    while file_lines and not file_lines[0].strip():
        file_lines.pop(0)
    agenda, start = [], 0
    if file_lines and file_lines[0].strip() == AGENDA:
        end = next((n for n, s in enumerate(file_lines) if s.strip() == NOTES), len(file_lines))
        agenda = [AgendaItem(p[2].strip(), p[1] != " ") for s in file_lines[1:end] if (p := ITEM.match(s.strip()))]
        start = end + 1
    elif file_lines and file_lines[0].strip() == NOTES:
        start = 1
    lines = [_line(s) for s in file_lines[start:]]
    while lines and not lines[-1].text.strip():
        lines.pop()
    while start and lines and not lines[0].text.strip():  # empty line after NOTES
        lines.pop(0)
    return Notes(tuple(agenda), tuple(lines))


def _line(text: str) -> NoteLine:
    minute = MINUTE.match(text)
    return NoteLine(text[minute.end() :] if minute else text, transcript.seconds_list(minute) if minute else None)


def to_text(notes: Notes) -> str:
    """Section headings are written when there is an agenda or the first line itself looks like a section heading."""
    parts = []
    first = next((s.text.strip() for s in notes.lines if s.text.strip()), "")
    if notes.agenda or first in (AGENDA, NOTES):
        if notes.agenda:
            parts += [AGENDA, "", *(f"- [{'x' if p.discussed else ' '}] {p.text}" for p in notes.agenda), ""]
        parts += [NOTES, ""]
    parts += [
        f"[{transcript.time(s.second)}] {s.text}" if s.second is not None and s.text.strip() else s.text
        for s in notes.lines
    ]
    return "\n".join(parts).rstrip() + "\n"


def read(folder: pathlib.Path) -> Notes:
    """Meeting notes, read only: old one-line "✎" notes stored with the marks come from there until move_old() or the
    next write moves them."""
    file = folder / FILE
    return from_text(file.read_text(encoding="utf-8")) if file.exists() else Notes(lines=_old_lines(folder))


def write(folder: pathlib.Path, notes: Notes) -> None:
    """Empty notes mean no file. Old notes go from the marks: they are in these notes now."""
    file = folder / FILE
    with transcript.locked(file):
        if notes.empty:
            file.unlink(missing_ok=True)
        else:
            transcript.write_text(file, to_text(notes))
        _drop_old_marks(folder)


def move_old(folder: pathlib.Path) -> None:
    """Old notes from the marks move to заметки.md, the transcript is rebuilt without them. Checked without the lock
    first: with_minutes() calls it for every display of the transcript."""
    if (folder / FILE).exists() or not _old_lines(folder):
        return
    with transcript.locked(folder / FILE):
        if not (folder / FILE).exists() and (old_lines := _old_lines(folder)):
            write(folder, Notes(lines=old_lines))


def _edit_lines(folder: pathlib.Path, change: Callable[[list[NoteLine]], list[NoteLine]]) -> None:
    """Read-change-write of the note lines under the meeting folder lock: two edits at once keep both."""
    with transcript.locked(folder / FILE):
        move_old(folder)
        previous_items = read(folder)
        write(folder, dataclasses.replace(previous_items, lines=tuple(change(list(previous_items.lines)))))


def add(folder: pathlib.Path, second: float, text: str) -> None:
    """A note line at a point of the recording: after the meeting, by right-clicking a transcript phrase."""

    def added(lines: list[NoteLine]) -> list[NoteLine]:
        place = next((n for n, s in enumerate(lines) if s.second is not None and s.second > second), len(lines))
        return [*lines[:place], NoteLine(text.strip(), int(second)), *lines[place:]]  # before the first later line

    _edit_lines(folder, added)


def delete(folder: pathlib.Path, second: float, text: str) -> None:
    _edit_lines(folder, lambda lines: [s for s in lines if (s.second, s.text.strip()) != (int(second), text.strip())])


def with_minutes(folder: pathlib.Path) -> list[tuple[int, str]]:
    """Lines with a recording minute, for the transcript: the note takes its place among the phrases. Old notes move
    out of the marks first, so they are not shown twice."""
    move_old(folder)
    return [(s.second, s.text.strip()) for s in read(folder).lines if s.second is not None and s.text.strip()]


def _old_lines(folder: pathlib.Path) -> tuple[NoteLine, ...]:
    return tuple(
        NoteLine(m["текст"].removeprefix(transcript.NOTE).strip(), int(m["время"]))
        for m in transcript.labels(folder)
        if m["текст"].startswith(transcript.NOTE)
    )


def _drop_old_marks(folder: pathlib.Path) -> None:
    """Under the lock write() holds: the marks and the transcript are in the same folder."""
    labels = transcript.labels(folder)
    kept = [m for m in labels if not m["текст"].startswith(transcript.NOTE)]
    if len(kept) != len(labels):
        transcript.write_json(folder / transcript.MARKS, kept)
        if (folder / transcript.PHRASES).exists():
            transcript.collect(folder)


def for_model(folder: pathlib.Path) -> list[str]:
    """Model input lines: the agenda (what the analyst wanted to discuss) and the analyst's notes with minutes."""
    notes = read(folder)
    result = []
    if notes.agenda:
        result += [
            "",
            "Повестка встречи (вопросы и темы аналитика; [x] - обсудили)",
            *(f"- [{'x' if p.discussed else ' '}] {p.text}" for p in notes.agenda),
        ]
    lines = [s for s in notes.lines if s.text.strip()]
    if lines:
        result += [
            "",
            "Заметки аналитика (минута - когда начал писать)",
            *(f"[{transcript.time(s.second)}] {s.text}" if s.second is not None else s.text for s in lines),
        ]
    return result


# --- meeting prep --------------------------------------------------------------------------------------------


def last_meeting(recordings: pathlib.Path, project: str, except_: pathlib.Path | None = None) -> catalog.Meeting | None:
    meetings = [
        v for v in catalog.list_(recordings) if v.project == project and v.folder != except_ and v.folder.is_dir()
    ]
    return meetings[0] if meetings else None


def undiscussed(recordings: pathlib.Path, project: str, except_: pathlib.Path | None = None) -> list[str]:
    """Agenda items of the project's previous meeting that were not reached."""
    previous_meeting = last_meeting(recordings, project, except_)
    return [p.text for p in read(previous_meeting.folder).agenda if not p.discussed] if previous_meeting else []


def analyst_questions(recordings: pathlib.Path, project: str) -> list[str]:
    """What the analyst prepared for the next meeting: every line of "Meeting prep" and the questions from "About the
    project"."""
    custom_items = [
        s.strip().removeprefix("- ").strip() for s in projects.for_meeting(recordings, project).splitlines()
    ]
    from_info = [
        s.strip().removeprefix("- ").strip()
        for s in projects.notes(recordings, project).text.splitlines()
        if question(s)
    ]
    return [s for s in custom_items + from_info if s]


def agenda(
    recordings: pathlib.Path, project: str, except_: pathlib.Path | None = None, from_board: tuple[str, ...] = ()
) -> list[AgendaItem]:
    """The next meeting's agenda without repeats: the analyst's questions, what was not discussed last time, and
    questions and decisions awaiting confirmation from the board."""
    result, before = [], set()
    for text in [*analyst_questions(recordings, project), *undiscussed(recordings, project, except_), *from_board]:
        key = text.casefold().rstrip(".?! ")
        if key and key not in before:
            before.add(key)
            result.append(AgendaItem(text))
    return result


def close_agenda(recordings: pathlib.Path, project: str, notes: Notes) -> None:
    """After the meeting, discussed questions leave "Meeting prep", the rest stay."""
    discussed = {p.text.casefold() for p in notes.agenda if p.discussed}
    lines = projects.for_meeting(recordings, project).splitlines()
    keep = [s for s in lines if s.strip().removeprefix("- ").strip().casefold() not in discussed]
    if len(keep) != len(lines):
        projects.set_for_meeting(recordings, project, "\n".join(keep).strip())


def agenda_text(items: list[AgendaItem], heading: str) -> str:
    """The agenda as a message to participants before the meeting."""
    return "\n".join([heading, "", *(f"{n}. {p.text}" for n, p in enumerate(items, 1))]) + "\n"
