# SPDX-License-Identifier: GPL-3.0-only
"""Spending log: each paid request to a service is a line with the day, meeting and model. The summary counts money
by it: repeated minutes or recognition add an expense, a deleted meeting does not take its expenses away."""

import dataclasses
import datetime
import json
import logging
import math
import pathlib
import uuid
from collections.abc import Iterator

from knotes import catalog, disk_names, providers, transcript

FILE = pathlib.Path(".узелок", "траты.jsonl")
RESERVE = pathlib.Path(".траты-резерв.jsonl")
PENDING = pathlib.Path(".неучтённые-расходы.jsonl")
JOURNALS = (FILE, RESERVE, PENDING)
log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Expense:
    when: datetime.datetime
    meeting: str  # meeting folder name
    kind: str  # catalog.SPEECH or catalog.MINUTES_KIND
    model: str
    how_many: float
    project: str = ""  # the meeting's current project, for a deleted one as of the expense
    title: str = ""
    receipt: str = ""
    currency: str = "USD"


def from_meetings(meetings: list[catalog.Meeting]) -> list[Expense]:
    """Expenses at the prices saved in the meetings, on the meeting day: this is the count while there is no log, and
    the log starts with them."""
    return [
        Expense(v.date, v.folder.name, kind, model, how_many, v.project, v.custom_title, currency=currency)
        for v in meetings
        if v.status != catalog.FILE
        for kind, model, how_many, currency in v.native_expenses
    ]


def _to_line(expense: Expense) -> str:
    fields = dataclasses.asdict(expense)
    fields["when"] = expense.when.isoformat(timespec="seconds")
    if expense.currency == "USD":
        del fields["currency"]
    if not expense.receipt:
        del fields["receipt"]
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
            str(fields.get("платеж", "")),
            str(fields.get("валюта") or "USD"),
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
    """File stamps used to refresh the open expense summary."""
    stamps = []
    for name in JOURNALS:
        try:
            info = (recordings / name).stat()
        except OSError:
            stamps.append(None)
        else:
            stamps.append((info.st_mtime_ns, info.st_size))
    return tuple(stamps)


def list_(recordings: pathlib.Path, meetings: list[catalog.Meeting]) -> list[Expense]:
    """Read charges once, including receipts that could not yet reach either journal."""
    pending = recordings / PENDING
    if pending.is_file():
        try:
            with transcript.locked(recordings / RESERVE):
                _settle(recordings)
        except OSError as error:
            log.warning("cannot reconcile receipts in %s: %s", recordings, error)
    exists = {v.folder.name: v for v in meetings}
    result = []
    seen = set()
    for name in (PENDING, FILE, RESERVE):
        for expense in _rows(_content(recordings / name)):
            if expense.receipt and expense.receipt in seen:
                continue
            seen.add(expense.receipt)
            if (meeting := exists.get(expense.meeting)) is not None:
                expense = dataclasses.replace(expense, project=meeting.project, title=meeting.custom_title)
            result.append(expense)
    return sorted(result, key=lambda expense: expense.when.isoformat()) or from_meetings(meetings)


def write(
    folder: pathlib.Path,
    kind: str,
    model: str,
    how_many: float | providers.Charge,
    when: datetime.datetime | None = None,
) -> None:
    """Writes a meeting expense to the log of its recordings folder. The first expense starts the log with the prices
    already saved in the meetings, so write it before the new price goes into the meeting."""
    charge = how_many if isinstance(how_many, providers.Charge) else providers.Charge(how_many)
    if not math.isfinite(charge.amount) or charge.amount <= 0:
        return
    properties = catalog.properties(folder)
    title = properties.get("название", "")
    add(
        folder.parent,
        Expense(
            when or datetime.datetime.now(),
            folder.name,
            kind,
            model,
            charge.amount,
            properties.get("проект", ""),
            title,
            currency=charge.currency,
        ),
    )


@dataclasses.dataclass
class SpeechPayment:
    folder: pathlib.Path

    def __call__(self, model: str, amount: float) -> None:
        write(self.folder, catalog.SPEECH, model, amount)

    def record(self, part: dict) -> None:
        properties = catalog.properties(self.folder)
        expense = Expense(
            datetime.datetime.fromisoformat(part["дата"]),
            self.folder.name,
            catalog.SPEECH,
            part["модель"],
            part["сумма"],
            properties.get("проект", ""),
            properties.get("название", ""),
            part["платеж"],
            part.get("валюта", "USD"),
        )
        add(self.folder.parent, expense)


def _receipts(contents: dict) -> set[str]:
    return {row.receipt for value in contents.values() if isinstance(value, str) for row in _rows(value) if row.receipt}


def _store(recordings: pathlib.Path, contents: dict, line: str) -> None:
    primary, reserve = recordings / FILE, recordings / RESERVE
    try:
        primary.parent.mkdir(exist_ok=True)
        _append(primary, contents[primary], line)
    except (OSError, UnicodeError) as error:
        log.warning("could not write the spending log, using the reserve: %s", error)
        _append(reserve, contents[reserve], line)


def _settle(recordings: pathlib.Path) -> None:
    pending = recordings / PENDING
    contents = _journal_contents(recordings / FILE, recordings / RESERVE)
    try:
        known = _receipts(contents)
        missing = "".join(_to_line(e) + "\n" for e in _rows(_content(pending)) if e.receipt not in known)
        if missing:
            _store(recordings, contents, missing)
        pending.unlink(missing_ok=True)
    except (OSError, UnicodeError) as error:
        log.warning("expense receipts waiting for the journal: %s", error)


def _seed(contents: dict, recordings: pathlib.Path) -> str:
    if any(not isinstance(value, str) or next(_rows(value), None) is not None for value in contents.values()):
        return ""
    lines = []
    for row in from_meetings(catalog.list_(recordings)):
        key = uuid.uuid5(uuid.NAMESPACE_URL, _to_line(row)).hex
        lines.append(_to_line(dataclasses.replace(row, receipt=key)) + "\n")
    return "".join(lines)


def add(recordings: pathlib.Path, expense: Expense) -> None:
    """Stage the receipt before acknowledging it. Replaying it after a crash is idempotent."""
    if not math.isfinite(expense.how_many) or expense.how_many <= 0:
        return
    if not expense.receipt:
        expense = dataclasses.replace(expense, receipt=uuid.uuid4().hex)
    primary, reserve, pending = (recordings / name for name in JOURNALS)
    with transcript.locked(reserve):
        contents = _journal_contents(primary, reserve, pending)
        if expense.receipt in _receipts(contents):
            return
        line = _seed(contents, recordings) + _to_line(expense) + "\n"
        try:
            _append(pending, contents[pending], line)
        except (OSError, UnicodeError):
            _store(recordings, contents, line)
        else:
            _settle(recordings)


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
