# SPDX-License-Identifier: GPL-3.0-only
"""Spending log: each paid request to a service is a line with the day, meeting and model. The summary counts money
by it: repeated minutes or recognition add an expense, a deleted meeting does not take its expenses away."""

import dataclasses
import datetime
import json
import logging
import math
import pathlib

from knotes import catalog, disk_names, transcript

FILE = pathlib.Path(".узелок", "траты.jsonl")  # in the recordings folder; one line per expense, new ones appended
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


def list_(recordings: pathlib.Path, meetings: list[catalog.Meeting]) -> list[Expense]:
    """All expenses of a recordings folder; project and title as the meeting has them now, for a deleted one as of
    the expense. No log yet: expenses by the meeting prices."""
    path = recordings / FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return from_meetings(meetings)
    exists = {v.folder.name: v for v in meetings}
    result = []
    for line in lines:
        expense = _from_line(line) if line.strip() else None
        if expense is None:
            continue
        if (meeting := exists.get(expense.meeting)) is not None:
            expense = dataclasses.replace(expense, project=meeting.project, title=meeting.custom_title)
        result.append(expense)
    return result or from_meetings(meetings)


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
    """An expense to the log; one of no meeting (the meeting prep) has an empty meeting and its own title. Under the
    folder lock: the window's background threads and the MCP server append one at a time, and the log is started
    once."""
    if not math.isfinite(expense.how_many) or expense.how_many <= 0:
        return
    path = recordings / FILE
    path.parent.mkdir(exist_ok=True)
    with transcript.locked(path):
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        if not any(_from_line(line) for line in content.splitlines() if line.strip()):
            previous = from_meetings(catalog.list_(recordings))
            content = "".join(_to_line(item) + "\n" for item in previous)
        if content and not content.endswith("\n"):
            content += "\n"
        transcript.write_text(path, content + _to_line(expense) + "\n")
