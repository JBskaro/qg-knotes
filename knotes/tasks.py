# SPDX-License-Identifier: GPL-3.0-only
"""Task board: tasks, remarks, open questions and tentative decisions from meeting minutes, plus tasks added by hand,
in columns. Each card has a source: the meeting, date and minute it came from. Which minutes sections go to the board
is set in the section descriptions (meeting_kinds.json).

A card's state is the base of its column: "To do", "In progress" or "Done". A project may have its own columns, each
counting as one of these bases (projects.py); a card keeps the key of its column, and a card whose column is gone or
counts as another state goes to the first column of its state. The board of all projects has the three base columns.

Any card has a description, assignee, due date, labels, priority, checklist, place in the column and archive flag.
A minutes item is recognized by meeting, section and text: its status and edits survive even if the minutes are
written again with the same items, and the minutes themselves do not change: the edits are stored separately.
Everything is stored in the recordings folder (.узелок/задачи.json): item statuses, item edits, own tasks and labels.
A broken card or minutes file is skipped with a note in the log: the rest of the board opens.
"""

import bisect
import dataclasses
import datetime
import functools
import logging
import pathlib
import re
import uuid
from collections.abc import Callable

from knotes import catalog, disk_names, kinds, language, minutes, notes, projects, transcript

FILE = pathlib.Path(".узелок", "задачи.json")
TODO, IN_PROGRESS, DONE = projects.BASES
# a decision is tentative and needs confirmation
TASK, QUESTION, DECISION = language.tr_key("задача"), language.tr_key("вопрос"), language.tr_key("решение")
STATES = projects.BASES
PRIORITIES = {
    "": language.tr_key("Без приоритета"),
    "срочный": language.tr_key("Срочный"),
    "высокий": language.tr_key("Высокий"),
    "средний": language.tr_key("Средний"),
    "низкий": language.tr_key("Низкий"),
}
OVERDUE, THIS_WEEK, NO_DUE_DATE = "просрочено", "на неделе", "без срока"  # due date filters
DUES = {
    OVERDUE: language.tr_key("Просрочено"),
    THIS_WEEK: language.tr_key("На этой неделе"),
    NO_DUE_DATE: language.tr_key("Без срока"),
}
OWN_PREFIX = "свои|"  # key prefix of a task added by hand
MANUAL = language.tr_key("добавлена вручную")  # source of an own task
NO_DUE = datetime.date.min  # in an edit: remove the due date
LABEL_COLORS = 7  # as many as project colors
FROM_MINUTES = ("текст", "кто", "срок")  # a minutes item has its own values: an empty edit is kept, not reverted
DATE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
BROKEN = (ValueError, TypeError, KeyError, AttributeError)  # what a hand-edited or cut-off card gives when read
log = logging.getLogger(__name__)


class GoneError(ValueError):
    """The card is no longer on the board: its minutes were written again without it, or it was deleted."""


@dataclasses.dataclass(frozen=True)
class Source:
    folder: pathlib.Path | None  # meeting; None if the task was added by hand
    title: str
    date: datetime.date
    second: int | None = None


@dataclasses.dataclass(frozen=True)
class ChecklistItem:
    text: str
    is_done: bool = False


@dataclasses.dataclass(frozen=True)
class Label:
    name: str
    color: int  # color number, as for projects


@dataclasses.dataclass(frozen=True)
class Task:
    key: str
    text: str
    who: str
    due: datetime.date | None
    kind: str  # task, question or tentative decision
    project: str
    status: str
    origin: Source
    description: str = ""
    labels: tuple[str, ...] = ()
    priority: str = ""
    order: int | None = None  # place in the column set by dragging; None means by due date
    archive: bool = False
    checklist: tuple[ChecklistItem, ...] = ()
    column: str = ""  # key of the column the card is in, among the columns of its project

    @property
    def custom(self) -> bool:
        return self.key.startswith(OWN_PREFIX)

    @property
    def opened(self) -> bool:
        """Not done yet and not archived: such cards go into the meeting agenda and the open items count."""
        return self.status != DONE and not self.archive

    def overdue(self, today: datetime.date) -> bool:
        """A question's date is when it is discussed, not a due date: only a task can be overdue."""
        return self.kind == TASK and self.due is not None and self.due < today and self.status != DONE

    def soon(self, today: datetime.date) -> bool:
        """The task is due within the next week."""
        return self.kind == TASK and self.due is not None and self.status != DONE and 0 <= (self.due - today).days <= 7


@dataclasses.dataclass(frozen=True)
class Edit:
    """What to change in a card; None leaves the field as is, due NO_DUE removes the due date, labels and checklist
    are replaced whole. column is the key of a project column, with status its base."""

    text: str | None = None
    description: str | None = None
    who: str | None = None
    due: datetime.date | None = None
    labels: tuple[str, ...] | None = None
    priority: str | None = None
    status: str | None = None
    order: int | None = None
    archive: bool | None = None
    checklist: tuple[ChecklistItem, ...] | None = None
    column: str | None = None


@dataclasses.dataclass(frozen=True)
class Filter:
    """What to show on the board; None means no filter. archive: only archived cards, otherwise only non-archived
    ones."""

    who: str | None = None
    label: str | None = None
    priority: str | None = None
    due: str | None = None  # OVERDUE, THIS_WEEK or NO_DUE_DATE
    text: str = ""
    archive: bool = False


def _data(recordings: pathlib.Path) -> dict:
    return transcript.read_json(recordings / FILE, {})


def _write(recordings: pathlib.Path, data: dict) -> None:
    (recordings / FILE).parent.mkdir(parents=True, exist_ok=True)
    transcript.write_json(recordings / FILE, data)


def _part(data: dict, name: str, kind: type) -> list | dict:
    """A part of the board file; one of the wrong type (edited by hand) reads as empty."""
    value = data.get(name)
    return value if isinstance(value, kind) else kind()


def who_and_due(who: str) -> tuple[str, datetime.date | None]:
    """Splits "Igor, 16.10.2026." into ("Igor", 16.10.2026); no date means no due date."""
    found = DATE.search(who)
    try:
        due = datetime.date(int(found[3]), int(found[2]), int(found[1])) if found else None
    except ValueError:  # 31.02.2026 is not a date
        due = None
    return DATE.sub("", who).strip(" ,."), due


def _from_data(data: dict) -> dict:
    """Card fields set in the data: own tasks and minutes item edits store them the same way. A wrong value raises
    one of BROKEN."""
    fields = {p: data[p] for p in ("текст", "описание", "кто", "приоритет", "столбец") if p in data}
    if not all(isinstance(z, str) for z in fields.values()):
        raise TypeError("a text field holds no text")
    if data.get("порядок") is not None and type(data["порядок"]) is not int:
        raise TypeError("the place in the column is not a number")
    fields |= {p: data[p] for p in ("порядок", "архив") if p in data}
    if "срок" in data:
        fields["срок"] = datetime.date.fromisoformat(data["срок"]) if data["срок"] else None
    if "метки" in data:
        fields["метки"] = tuple(str(i) for i in data["метки"])
    if "чек_лист" in data:
        fields["чек_лист"] = tuple(ChecklistItem(str(p["текст"]), bool(p.get("сделан"))) for p in data["чек_лист"])
    return disk_names.from_disk(fields)


def _deduplicated(names: tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys(i.strip() for i in names if i.strip()))


def _to_data(edit_item: Edit) -> dict:
    """An edit as fields for the file; an invalid one is rejected and nothing is written."""
    if edit_item.text is not None and not edit_item.text.strip():
        raise ValueError(language.tr("The task needs a text"))
    if edit_item.status is not None and edit_item.status not in STATES:
        raise ValueError(language.tr("Status must be one of: {list_}", list_=", ".join(STATES)))
    if edit_item.priority is not None and edit_item.priority not in PRIORITIES:
        raise ValueError(language.tr("Priority must be one of: {list_}", list_=", ".join(p for p in PRIORITIES if p)))
    fields = disk_names.to_disk({p: getattr(edit_item, p) for p in _FIELDS if getattr(edit_item, p) is not None})
    for field in ("текст", "кто"):
        if field in fields:
            fields[field] = fields[field].strip()
    if "срок" in fields:
        fields["срок"] = None if edit_item.due == NO_DUE else fields["срок"].isoformat()
    if "метки" in fields:
        fields["метки"] = _deduplicated(fields["метки"])
    if "чек_лист" in fields:
        fields["чек_лист"] = [disk_names.to_disk(dataclasses.asdict(p)) for p in fields["чек_лист"] if p.text.strip()]
    return fields


_FIELDS = tuple(f.name for f in dataclasses.fields(Edit))


def _empty(value: object) -> bool:
    return value is None or value is False or value in ("", [])


def _own_task(data: dict, key: str) -> dict:
    found = next(
        (d for d in _part(data, "свои", list) if isinstance(d, dict) and OWN_PREFIX + str(d.get("ид")) == key), None
    )
    if found is None:
        raise GoneError(language.tr("No task “{key}”", key=key))
    return found


def _change(data: dict, key: str, fields: dict) -> None:
    """Puts the fields into an own task or into the edits of a minutes item (its status goes to the separate statuses
    map). A card moved to another column without a place goes to the top, a card of a new state without a column to
    the first column of the state; an empty value is not stored, except for the fields taken from the minutes."""
    status = fields.pop("состояние", None)
    custom = key.startswith(OWN_PREFIX)
    if custom:
        target = _own_task(data, key)
        before = target.get("состояние", TODO)
    else:
        target = data.setdefault("правки", {}).setdefault(key, {})
        before = data.get("состояния", {}).get(key, TODO)
    if status not in (None, before):
        fields.setdefault("столбец", "")
    if status not in (None, before) or fields.get("столбец", target.get("столбец", "")) != target.get("столбец", ""):
        fields.setdefault("порядок", None)
    if status not in (None, before):
        if custom:
            target["состояние"] = status
        else:
            data.setdefault("состояния", {})[key] = status
    for field, value in fields.items():
        if _empty(value) and field not in FROM_MINUTES:
            target.pop(field, None)
        else:
            target[field] = value
    if not custom and not target:
        data["правки"].pop(key)


def _create_labels(data: dict, names: list[str]) -> None:
    """Labels that do not exist yet are created with the next color."""
    list_ = data.setdefault("метки", []) if names else []
    for name in names:
        if all(m.get("имя") != name for m in list_ if isinstance(m, dict)):
            list_.append({"имя": name, "цвет": len(list_) % LABEL_COLORS})


@functools.lru_cache(maxsize=4096)
def _board_items(file: pathlib.Path, _file_key: tuple[int, int]) -> tuple[tuple, ...]:
    """(kind, section heading, text, who, due date, second) of the minutes items that go to the board, re-read only
    when the file changed (mtime and size)."""
    result = []
    for section in minutes.sections(file.read_text(encoding="utf-8"))[1]:
        board = kinds.description(section.heading).board
        for item in (p for p in minutes.items(section) if p.number):
            confirms = minutes.tentative(item.who)
            kind = board or (DECISION if confirms is not None else "")
            if kind:
                who, due = who_and_due(item.who if confirms is None else confirms)
                result.append((kind, section.heading, item.text, who, due, item.second))
    return tuple(result)


def _items_of(folder: pathlib.Path) -> tuple[tuple, ...]:
    file = folder / minutes.FILE
    try:
        info = file.stat()
    except FileNotFoundError:
        return ()
    return _board_items(file, (info.st_mtime_ns, info.st_size))


def _with_data(task: Task, data: object) -> Task:
    """The card with the fields stored for it; broken data raises one of BROKEN."""
    if not isinstance(data, dict):
        raise TypeError("the card data is not a dictionary")
    task = dataclasses.replace(task, **_from_data(data))
    if task.status not in STATES:
        raise ValueError(f"unknown state {task.status!r}")
    return task


def _skipped(make: Callable[[], Task], what: str) -> Task | None:
    """A card, or None if its data is broken: the rest of the board opens."""
    try:
        return make()
    except BROKEN as error:
        log.warning("skipped a broken card on the board (%s): %s", what, error)
        return None


def _minutes_card(values: tuple, project: str, origin: Source, data: dict) -> Task | None:
    kind, heading, text, who, due, _second = values
    key = f"{origin.folder.name}|{heading}|{text}"  # type: ignore[union-attr]
    task = Task(key, text, who, due, kind, project, _part(data, "состояния", dict).get(key, TODO), origin)
    return _skipped(lambda: _with_data(task, _part(data, "правки", dict).get(key, {})), key)


def _from_minutes(meeting: catalog.Meeting, data: dict) -> list[Task]:
    """Cards of the meeting minutes; minutes that cannot be read (not UTF-8, the disk failed) give none."""
    try:
        items = _items_of(meeting.folder)
    except (OSError, ValueError) as error:
        log.warning("skipped the minutes of %s on the board: %s", meeting.folder.name, error)
        return []
    date = meeting.date.date()
    cards = (
        _minutes_card(values, meeting.project, Source(meeting.folder, meeting.heading, date, values[5]), data)
        for values in items
    )
    return [z for z in cards if z is not None]


def _own(data: dict) -> Task:
    base_name = Task(
        OWN_PREFIX + str(data["ид"]),
        "",
        "",
        None,
        TASK,
        data.get("проект", ""),
        data.get("состояние", TODO),
        Source(None, MANUAL, datetime.date.fromisoformat(data["создана"])),
    )
    return _with_data(base_name, data)


def _placed(task: Task, layouts: dict[str, list[projects.Column]]) -> Task:
    """The card in its column if that one is still on the project board and is of the card's state, otherwise in
    the first column of the state."""
    columns = layouts.get(task.project) or projects.BASE_COLUMNS
    if any(c.key == task.column and c.base == task.status for c in columns):
        return task
    return dataclasses.replace(task, column=next(c.key for c in columns if c.base == task.status))


def tasks(
    recordings: pathlib.Path, project: str | None = None, meetings: list[catalog.Meeting] | None = None
) -> list[Task]:
    """Tasks from the minutes (newest meetings first) and own tasks, archived included; with project, only that
    project's. meetings: the meeting list just read, so the folder is not read again."""
    data = _data(recordings)
    result: list[Task | None] = [
        z
        for v in (catalog.list_(recordings) if meetings is None else meetings)
        if v.status == catalog.READY and project in (None, v.project)
        for z in _from_minutes(v, data)
    ]
    for custom in _part(data, "свои", list):
        if isinstance(custom, dict) and project in (None, custom.get("проект", "")):
            result.append(_skipped(lambda c=custom: _own(c), "own task"))
    layouts = projects.all_columns(recordings)
    return [_placed(z, layouts) for z in result if z is not None]


def refreshed(recordings: pathlib.Path, task: Task) -> Task | None:
    """The card as stored now, read without the whole board (after an edit in the card panel); None if it is gone."""
    data = _data(recordings)
    if task.custom:
        try:
            found = _own_task(data, task.key)
        except GoneError:
            return None
        fresh = _skipped(lambda: _own(found), task.key)
    else:
        folder = task.origin.folder or recordings
        try:
            items = _items_of(folder)
        except (OSError, ValueError):
            return None
        values = next((z for z in items if f"{folder.name}|{z[1]}|{z[2]}" == task.key), None)
        fresh = values and _minutes_card(values, task.project, task.origin, data)
    return _placed(fresh, projects.all_columns(recordings)) if fresh else None


def source_title(source: Source) -> str:
    """The title of the card's meeting on screen, without reading the meeting: a default title is shown in the
    interface language."""
    if source.folder is None:
        return language.tr(source.title)
    default = catalog.default_title(source.folder)
    return catalog.default_title(source.folder, view_state=True) if source.title == default else source.title


def agenda(recordings: pathlib.Path, project: str, except_: pathlib.Path | None = None) -> list[notes.AgendaItem]:
    """Agenda of the project's next meeting: the analyst's questions, what was not discussed last time, open
    questions and tentative decisions from the board."""
    from_board = tuple(z.text for z in tasks(recordings, project) if z.kind != TASK and z.opened)
    return notes.agenda(recordings, project, except_=except_, from_board=from_board)


def in_order(list_: list[Task], today: datetime.date) -> list[Task]:
    """Cards of a column: first those without a place set by hand (overdue first, then by due date, newer above),
    then those with a place, in place order."""

    def place(task: Task) -> tuple:
        if task.order is not None:
            return 1, task.order, 0, 0
        return 0, not task.overdue(today), (task.due or datetime.date.max).toordinal(), -task.origin.date.toordinal()

    return sorted(list_, key=place)


def _due_matches(task: Task, due: str | None, today: datetime.date) -> bool:
    if due == OVERDUE:
        return task.overdue(today)
    if due == THIS_WEEK:  # from today through Sunday
        return task.due is not None and 0 <= (task.due - today).days <= 6 - today.weekday()
    return due != NO_DUE_DATE or task.due is None


def _for_search(task: Task) -> str:
    return catalog.for_search(
        " ".join(
            (task.text, task.description, task.who, *task.labels, *(p.text for p in task.checklist), task.origin.title)
        )
    )


def pick_out(list_: list[Task], filter_: Filter, today: datetime.date) -> list[Task]:
    query = catalog.for_search(filter_.text.strip())
    return [
        z
        for z in list_
        if z.archive == filter_.archive
        and filter_.who in (None, z.who)
        and filter_.label in (None, *z.labels)
        and filter_.priority in (None, z.priority)
        and _due_matches(z, filter_.due, today)
        and query in _for_search(z)
    ]


def assignees(list_: list[Task]) -> list[str]:
    """Who does the tasks on the board, alphabetically; a question or decision has no assignee, only who will
    answer."""
    return sorted({z.who for z in list_ if z.who and z.kind == TASK}, key=str.lower)


def _exists(recordings: pathlib.Path, key: str) -> bool:
    """The minutes item is still in its minutes."""
    folder = recordings / key.partition("|")[0]
    try:
        return any(f"{folder.name}|{z[1]}|{z[2]}" == key for z in _items_of(folder))
    except (OSError, ValueError):
        return False


@transcript.locked_edit(FILE)
def edit(recordings: pathlib.Path, key: str, edit_item: Edit) -> None:
    """An own task changes in place, a minutes item gets a separate edit; new labels are created. A card that is no
    longer on the board raises GoneError: the edit is not written to nowhere."""
    fields = _to_data(edit_item)
    if not key.startswith(OWN_PREFIX) and not _exists(recordings, key):
        raise GoneError(language.tr("This card is no longer on the board: its minutes were written again without it"))
    data = _data(recordings)
    _change(data, key, fields)
    _create_labels(data, fields.get("метки", []))
    _write(recordings, data)


@transcript.locked_edit(FILE)
def reorder(recordings: pathlib.Path, status: str, keys: list[str], column: str | None = None) -> None:
    """Puts the cards into the column of the state in this order: after a card is dragged, the column remembers the
    layout. column is the key of a project column of that state; None keeps a card that already has the state in its
    column."""
    if status not in STATES:
        raise ValueError(language.tr("Status must be one of: {list_}", list_=", ".join(STATES)))
    data = _data(recordings)
    for place, key in enumerate(keys):
        _change(data, key, {"состояние": status, "порядок": place} | ({} if column is None else {"столбец": column}))
    _write(recordings, data)


def placed(column: list[str], key: str, before: str | None = None, after: str | None = None) -> list[str]:
    """Keys of a whole column with the dragged card right before the before card, else right after the after card,
    else at the end. The column holds the cards a filter hides too: they keep their places among the others."""
    keys = [k for k in column if k != key]
    if before in keys:
        keys.insert(keys.index(before), key)
    elif after in keys:
        keys.insert(keys.index(after) + 1, key)
    else:
        keys.append(key)
    return keys


def kept(old: list[str], new: list[str]) -> set[str]:
    """Keys of the old order that keep their places when it becomes the new one: the longest run of them already in
    the new order. A board column moves only the other cards and keeps the rest as they are drawn."""
    position = {k: n for n, k in enumerate(new)}
    run = [k for k in old if k in position]
    values: list[int] = []  # the smallest last new position of a run of each length
    ends: list[int] = []  # where in run such a run ends
    back = [-1] * len(run)
    for number, key in enumerate(run):
        at = bisect.bisect_left(values, position[key])
        back[number] = ends[at - 1] if at else -1
        if at == len(values):
            values.append(position[key])
            ends.append(number)
        else:
            values[at], ends[at] = position[key], number
    result, number = set(), ends[-1] if ends else -1
    while number >= 0:
        result.add(run[number])
        number = back[number]
    return result


@transcript.locked_edit(FILE)
def add(recordings: pathlib.Path, text: str, project: str = "", edit_item: Edit | None = None) -> Task:
    """Adds an own task; the edit holds the other fields: who, due date, column, place."""
    fields = _to_data(dataclasses.replace(edit_item or Edit(), text=text))
    custom = {
        "ид": uuid.uuid4().hex[:10],
        "текст": "",
        "кто": "",
        "срок": None,
        "проект": project,
        "состояние": TODO,
        "создана": datetime.date.today().isoformat(),
    }
    data = _data(recordings)
    data.setdefault("свои", []).append(custom)
    _change(data, OWN_PREFIX + custom["ид"], fields)
    _create_labels(data, fields.get("метки", []))
    _write(recordings, data)
    return _placed(_own(custom), projects.all_columns(recordings))


@transcript.locked_edit(FILE)
def delete(recordings: pathlib.Path, key: str) -> None:
    """Only an own task: a minutes item is archived instead."""
    if not key.startswith(OWN_PREFIX):
        raise ValueError(language.tr("A task from the minutes can't be deleted, archive it instead"))
    data = _data(recordings)
    data["свои"] = [
        d for d in _part(data, "свои", list) if not isinstance(d, dict) or OWN_PREFIX + str(d.get("ид")) != key
    ]
    _write(recordings, data)


@transcript.locked_edit(FILE)
def rename_project(recordings: pathlib.Path, old_value: str, new_value: str) -> None:
    """Own tasks follow the project; tasks from the minutes follow their meetings on their own."""
    data = _data(recordings)
    for custom in _part(data, "свои", list):
        if isinstance(custom, dict) and custom.get("проект") == old_value:
            custom["проект"] = new_value
    _write(recordings, data)


# --- project columns -------------------------------------------------------------------------------------------


def _column(list_: list[projects.Column], key: str) -> projects.Column:
    found = next((c for c in list_ if c.key == key), None)
    if found is None:
        raise GoneError(language.tr("The column is no longer on the board"))
    return found


def _move_cards(recordings: pathlib.Path, project: str, key: str, to: projects.Column, keep_order: bool) -> None:
    """The cards of the key column go to the to column: at the top of it, or keeping their order."""
    cards = [z for z in tasks(recordings, project) if z.column == key]
    data = _data(recordings)
    for task in cards:
        _change(
            data, task.key, {"состояние": to.base, "столбец": to.key, "порядок": task.order if keep_order else None}
        )
    _write(recordings, data)


@transcript.locked_edit(FILE)
def delete_column(recordings: pathlib.Path, project: str, key: str, to: str) -> None:
    """Deletes a column of the project board; its cards go to the top of the to column."""
    list_ = projects.columns(recordings, project)
    column, target = _column(list_, key), _column(list_, to)
    if key == to:
        raise ValueError(language.tr("Choose another column for the cards"))
    projects.keep_base(list_, column)
    _move_cards(recordings, project, key, target, keep_order=False)
    projects.set_columns(recordings, project, [c for c in list_ if c.key != key])


@transcript.locked_edit(FILE)
def rebase_column(recordings: pathlib.Path, project: str, key: str, base: str) -> None:
    """Changes what the column counts as: its cards get the new state and stay in it, in their order."""
    if base not in STATES:
        raise ValueError(language.tr("Status must be one of: {list_}", list_=", ".join(STATES)))
    list_ = projects.columns(recordings, project)
    column = _column(list_, key)
    if column.base == base:
        return
    projects.keep_base(list_, column)
    moved = dataclasses.replace(column, base=base)
    _move_cards(recordings, project, key, moved, keep_order=True)
    projects.set_columns(recordings, project, [moved if c.key == key else c for c in list_])


def column_marks(recordings: pathlib.Path, list_: list[Task]) -> dict[str, str]:
    """For the board of all projects, where cards stand in the columns of their state: the name of the project
    column of each card, by card key; a card in a column named as its state has none."""
    layouts = projects.all_columns(recordings)
    result = {}
    for task in list_:
        column = next((c for c in layouts.get(task.project, ()) if c.key == task.column), None)
        if column is not None and projects.column_name(column) != language.tr(STATES[column.base]):
            result[task.key] = projects.column_name(column)
    return result


# --- labels ---------------------------------------------------------------------------------------------------


def _label(data: object) -> Label | None:
    try:
        return Label(str(data["имя"]), int(data.get("цвет", 0)) % LABEL_COLORS)  # type: ignore[index, union-attr]
    except BROKEN:
        log.warning("skipped a broken label: %r", data)
        return None


def labels(recordings: pathlib.Path) -> list[Label]:
    """Labels of the recordings folder in creation order: shared by all projects."""
    return [m for m in map(_label, _part(_data(recordings), "метки", list)) if m is not None]


def _new_name(data: dict, name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError(language.tr("The label needs a name"))
    if any(m.get("имя") == name for m in _part(data, "метки", list) if isinstance(m, dict)):
        raise ValueError(language.tr("Label “{name}” already exists", name=name))
    return name


@transcript.locked_edit(FILE)
def add_label(recordings: pathlib.Path, name: str, color: int | None = None) -> Label:
    """Without a color, the next one is used."""
    data = _data(recordings)
    name = _new_name(data, name)
    _create_labels(data, [name])
    if color is not None:
        data["метки"][-1]["цвет"] = color % LABEL_COLORS
    _write(recordings, data)
    return Label(name, data["метки"][-1]["цвет"])


def _cards(data: dict) -> list[dict]:
    """Data of the cards that can have labels: own tasks and minutes item edits."""
    return [d for d in (*_part(data, "свои", list), *_part(data, "правки", dict).values()) if isinstance(d, dict)]


def _labelled(card: dict) -> list:
    found = card.get("метки", [])
    return found if isinstance(found, list) else []


@transcript.locked_edit(FILE)
def rename_label(recordings: pathlib.Path, old_value: str, new_value: str) -> None:
    """The new name goes to every card with this label too."""
    if new_value.strip() == old_value:
        return
    data = _data(recordings)
    new_value = _new_name(data, new_value)
    for label in _part(data, "метки", list):
        if isinstance(label, dict) and label.get("имя") == old_value:
            label["имя"] = new_value
    for card in _cards(data):
        if old_value in _labelled(card):
            card["метки"] = [new_value if i == old_value else i for i in card["метки"]]
    _write(recordings, data)


@transcript.locked_edit(FILE)
def recolor_label(recordings: pathlib.Path, name: str, color: int) -> None:
    data = _data(recordings)
    for label in _part(data, "метки", list):
        if isinstance(label, dict) and label.get("имя") == name:
            label["цвет"] = color % LABEL_COLORS
    _write(recordings, data)


@transcript.locked_edit(FILE)
def delete_label(recordings: pathlib.Path, name: str) -> None:
    """The label is removed from all cards too."""
    data = _data(recordings)
    data["метки"] = [m for m in _part(data, "метки", list) if not isinstance(m, dict) or m.get("имя") != name]
    for card in _cards(data):
        if name in _labelled(card):
            card["метки"] = [i for i in card["метки"] if i != name]
    _write(recordings, data)
