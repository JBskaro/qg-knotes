# SPDX-License-Identifier: GPL-3.0-only
"""Spending log: each paid request to a service is a line with the day, meeting and model. The summary counts money
by it: repeated minutes or recognition add an expense, a deleted meeting does not take its expenses away."""

import dataclasses
import datetime
import json
import logging
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
        return Expense(
            datetime.datetime.fromisoformat(fields["когда"]),
            str(fields["встреча"]),
            str(fields["вид"]),
            str(fields.get("модель", "")),
            float(fields["сколько"]),
            str(fields.get("проект", "")),
            str(fields.get("название", "")),
        )
    except (ValueError, KeyError, TypeError):  # a cut-off write or a manual edit: the other expenses are intact
        log.warning("skipped a broken line of the spending log")
        return None


def list_(recordings: pathlib.Path, meetings: list[catalog.Meeting]) -> list[Expense]:
    """All expenses of a recordings folder; project and title as the meeting has them now, for a deleted one as of
    the expense. No log yet: expenses by the meeting prices."""
    path = recordings / FILE
    if not path.exists():
        return from_meetings(meetings)
    exists = {v.folder.name: v for v in meetings}
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        expense = _from_line(line) if line.strip() else None
        if expense is None:
            continue
        if (meeting := exists.get(expense.meeting)) is not None:
            expense = dataclasses.replace(expense, project=meeting.project, title=meeting.custom_title)
        result.append(expense)
    return result


def write(folder: pathlib.Path, kind: str, model: str, how_many: float, when: datetime.datetime | None = None) -> None:
    """Writes a meeting expense to the log of its recordings folder. The first expense starts the log with the prices
    already saved in the meetings, so write it before the new price goes into the meeting."""
    if how_many <= 0:
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
    if expense.how_many <= 0:
        return
    path = recordings / FILE
    path.parent.mkdir(exist_ok=True)
    start = [] if path.exists() else from_meetings(catalog.list_(recordings))  # before the lock: it reads every meeting
    with transcript.locked(path):
        previous_items = [] if path.exists() else start
        with path.open("a", encoding="utf-8") as file:
            file.write("".join(_to_line(tr) + "\n" for tr in [*previous_items, expense]))
