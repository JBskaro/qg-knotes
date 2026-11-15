# SPDX-License-Identifier: GPL-3.0-only
"""Spending log: each paid request to a service is a line with the day, meeting and model. The summary counts money
by it: repeated minutes or recognition add an expense, a deleted meeting does not take its expenses away."""

import dataclasses
import datetime
import json
import logging
import math
import pathlib
from collections.abc import Iterator

from knotes import catalog, disk_names, transcript

FILE = pathlib.Path(".узелок", "траты.jsonl")  # in the recordings folder; one line per expense, new ones appended
RESERVE = pathlib.Path(".траты-резерв.jsonl")
log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Expense:
    when: datetime.datetime
    meeting: str  # meeting folder name
    kind: str  # catalog.SPEECH or catalog.MINUTES_KIND
    model: str
    how_many: float
    project: str = ""  # the meeting's current project, for a deleted one as of the expense
    title: str = ""  # the meeting's own title as of the expense: a deleted one cannot be asked anymore


def from_meetings(meetings: list[catalog.Meeting]) -> list[Expense]:
    """Expenses at the prices saved in the meetings, on the meeting day: this is the count while there is no log, and
    the log starts with them."""
    return [
        Expense(v.date, v.folder.name, kind, model, how_many, v.project, v.custom_title)
        for v in meetings
        if v.status != catalog.FILE
        for kind, model, how_many in v.expenses
    ]


def _to_line(expense: Expense) -> str:
    fields = dataclasses.asdict(expense) | {
        "when": expense.when.isoformat(timespec="seconds"),
        "how_many": round(expense.how_many, 6),
    }
    return json.dumps(disk_names.to_disk(fields), ensure_ascii=False)


def _from_line(line: str) -> Expense | None:
    try:
        fields = json.loads(line)
        kind, amount = fields["вид"], float(fields["сколько"])
        if kind not in (catalog.SPEECH, catalog.MINUTES_KIND) or not math.isfinite(amount) or amount <= 0:
            raise ValueError("invalid expense")
        return Expense(
            datetime.datetime.fromisoformat(fields["когда"]),
            str(fields["встреча"]),
            kind,
            str(fields.get("модель", "")),
            amount,
            str(fields.get("проект", "")),
            str(fields.get("название", "")),
        )
    except (ValueError, KeyError, TypeError, OverflowError):
        log.warning("skipped a broken line of the spending log")
        return None


def _content(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _rows(content: str) -> Iterator[Expense]:
    return (expense for line in content.splitlines() if line.strip() and (expense := _from_line(line)))


def revision(recordings: pathlib.Path) -> tuple[tuple[int, int] | None, ...]:
    """Lightweight change check for the open summary, including the reserve journal."""
    stamps = []
    for name in (FILE, RESERVE):
        try:
            info = (recordings / name).stat()
        except OSError:
            stamps.append(None)
        else:
            stamps.append((info.st_mtime_ns, info.st_size))
    return tuple(stamps)


def list_(recordings: pathlib.Path, meetings: list[catalog.Meeting]) -> list[Expense]:
    """Both journals contain distinct charges. The meeting's current project and title take precedence."""
    exists = {v.folder.name: v for v in meetings}
    result = []
    for name in (FILE, RESERVE):
        for expense in _rows(_content(recordings / name)):
            if (meeting := exists.get(expense.meeting)) is not None:
                expense = dataclasses.replace(expense, project=meeting.project, title=meeting.custom_title)
            result.append(expense)
    return sorted(result, key=lambda expense: expense.when.isoformat()) or from_meetings(meetings)


def write(folder: pathlib.Path, kind: str, model: str, how_many: float, when: datetime.datetime | None = None) -> None:
    """Writes a meeting expense to the log of its recordings folder. The first expense starts the log with the prices
    already saved in the meetings, so write it before the new price goes into the meeting."""
    if not math.isfinite(how_many) or how_many <= 0:
        return
    properties = catalog.properties(folder)
    title = properties.get("название", "")
    add(
        folder.parent,
        Expense(
            when or datetime.datetime.now(), folder.name, kind, model, how_many, properties.get("проект", ""), title
        ),
    )


def add(recordings: pathlib.Path, expense: Expense) -> None:
    """Log each charge once. A failure to write the main journal falls back to a separate file."""
    if not math.isfinite(expense.how_many) or expense.how_many <= 0:
        return
    primary, reserve = recordings / FILE, recordings / RESERVE
    with transcript.locked(reserve):
        content = _journal_contents(primary, reserve)
        seed = ""
        if all(isinstance(value, str) and next(_rows(value), None) is None for value in content.values()):
            seed = "".join(_to_line(item) + "\n" for item in from_meetings(catalog.list_(recordings)))
        line = seed + _to_line(expense) + "\n"
        try:
            primary.parent.mkdir(exist_ok=True)
            _append(primary, content[primary], line)
        except (OSError, UnicodeError) as error:
            log.warning("could not write the spending log, using the reserve: %s", error)
            _append(reserve, content[reserve], line)


def _journal_contents(*files: pathlib.Path) -> dict[pathlib.Path, str | OSError | UnicodeError]:
    contents = {}
    for path in files:
        try:
            contents[path] = _content(path)
        except (OSError, UnicodeError) as error:
            contents[path] = error
    return contents


def _append(path: pathlib.Path, content: str | OSError | UnicodeError, line: str) -> None:
    if isinstance(content, Exception):
        raise content
    separator = "\n" if content and not content.endswith("\n") else ""
    transcript.write_text(path, content + separator + line)
