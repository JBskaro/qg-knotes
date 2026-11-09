# SPDX-License-Identifier: GPL-3.0-only
"""Project details: notes (database, agreements, terms, who is who), questions for the next meeting, the default
meeting kind and the task board columns.

Stored in the recordings folder next to the meetings (.узелок/проекты.json). With the checkbox on, the notes go to the
model when it writes the minutes; the AI assistant sees them too.

A board column counts as one of three bases: to do, in progress or done. Open tasks, the archive and the board of all
projects go by the base, so a project's own columns ("Waiting for the client", "In review") are a finer split of them.
A project without its own columns, as in files of older versions, has the three base ones.
"""

import dataclasses
import logging
import pathlib
import uuid

from knotes import disk_names, transcript
from knotes.language import tr, tr_key

FILE = pathlib.Path(".узелок", "проекты.json")
# column bases: the task states on disk and their names
BASES = {"сделать": tr_key("Сделать"), "в работе": tr_key("В работе"), "готово": tr_key("Готово")}
log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ProjectNotes:
    text: str = ""
    for_model: bool = True


@dataclasses.dataclass(frozen=True)
class Column:
    """A board column: a base column is keyed by its base, an added one by a generated key; without a name the
    column shows the name of its base."""

    key: str
    base: str
    name: str = ""


BASE_COLUMNS = tuple(Column(z, z) for z in BASES)


def _all_items(recordings: pathlib.Path) -> dict:
    return transcript.read_json(recordings / FILE, {})


def _project(recordings: pathlib.Path, project: str) -> dict:
    data = _all_items(recordings).get(project)
    return data if isinstance(data, dict) else {}


@transcript.locked_edit(FILE)
def _refresh(recordings: pathlib.Path, project: str, **fields: object) -> None:
    """Empty values are not stored, nor is a project without details."""
    all_ = _all_items(recordings)
    previous = all_.get(project) if isinstance(all_.get(project), dict) else {}
    data = {k: z for k, z in (previous | disk_names.to_disk(fields)).items() if z not in ("", None)}
    if data.keys() - {"для_модели"}:
        all_[project] = data
    else:
        all_.pop(project, None)
    (recordings / FILE).parent.mkdir(parents=True, exist_ok=True)
    transcript.write_json(recordings / FILE, all_)


def notes(recordings: pathlib.Path, project: str) -> ProjectNotes:
    data = _all_items(recordings).get(project) or {}
    return ProjectNotes(data.get("заметки", ""), data.get("для_модели", True))


def set_(recordings: pathlib.Path, project: str, new_items: ProjectNotes) -> None:
    _refresh(
        recordings,
        project,
        notes=new_items.text.strip(),
        for_model=new_items.for_model if new_items.text.strip() else None,
    )


def for_meeting(recordings: pathlib.Path, project: str) -> str:
    """Questions and topics the analyst prepares for the project's next meeting: one line per agenda item."""
    return (_all_items(recordings).get(project) or {}).get("к_встрече", "") if project else ""


def set_for_meeting(recordings: pathlib.Path, project: str, text: str) -> None:
    _refresh(recordings, project, for_meeting=text.strip())


def kind(recordings: pathlib.Path, project: str) -> str:
    """The project's default meeting kind; empty means not set."""
    return (_all_items(recordings).get(project) or {}).get("вид", "") if project else ""


def set_kind(recordings: pathlib.Path, project: str, kind_: str) -> None:
    _refresh(recordings, project, kind=kind_)


@transcript.locked_edit(FILE)
def rename(recordings: pathlib.Path, old_value: str, new_value: str) -> None:
    """The details move to the new project name; an empty name means the project is deleted, and so are its details."""
    all_ = _all_items(recordings)
    if old_value not in all_:
        return
    data = all_.pop(old_value)
    if new_value:
        all_[new_value] = data
    transcript.write_json(recordings / FILE, all_)


# --- board columns -----------------------------------------------------------------------------------------------


def _columns_from(stored: object) -> list[Column]:
    """Columns as stored; a broken entry is skipped, and a base left without a column gets its base column at the
    end: every base keeps a column, so a task of any state has a place on the board."""
    result: list[Column] = []
    for data in stored if isinstance(stored, list) else []:
        try:
            column = Column(str(data["ключ"]), str(data["основа"]), str(data.get("имя", "")))
        except (KeyError, TypeError, AttributeError):
            column = None
        if column is None or column.base not in BASES or any(c.key == column.key for c in result):
            log.warning("skipped a broken board column: %r", data)
            continue
        result.append(column)
    for base in BASES:
        if all(c.base != base for c in result):
            result.append(Column(base, base))
    return result


def columns(recordings: pathlib.Path, project: str) -> list[Column]:
    """The project's board columns in order, from left to right."""
    return _columns_from(_project(recordings, project).get("столбцы"))


def all_columns(recordings: pathlib.Path) -> dict[str, list[Column]]:
    """Columns of the projects that have their own; the others have the base ones."""
    return {
        project: _columns_from(data["столбцы"])
        for project, data in _all_items(recordings).items()
        if isinstance(data, dict) and "столбцы" in data
    }


def set_columns(recordings: pathlib.Path, project: str, list_: list[Column]) -> None:
    """The base columns as they are by default are not stored."""
    stored = (
        None
        if tuple(list_) == BASE_COLUMNS
        else [disk_names.to_disk({k: z for k, z in dataclasses.asdict(c).items() if z}) for c in list_]
    )
    _refresh(recordings, project, columns=stored)


def column_name(column: Column) -> str:
    """The name on the board, in the interface language for a base column that was not renamed."""
    return column.name or tr(BASES[column.base])


def _name(list_: list[Column], name: str, key: str = "") -> str:
    """A column name: not empty and not the name of another column of the board."""
    name = " ".join(name.split())
    if not name:
        raise ValueError(tr("The column needs a name"))
    if any(column_name(c).casefold() == name.casefold() and c.key != key for c in list_):
        raise ValueError(tr("Column “{name}” already exists", name=name))
    return name


def _index(list_: list[Column], key: str) -> int:
    number = next((n for n, c in enumerate(list_) if c.key == key), None)
    if number is None:
        raise ValueError(tr("The column is no longer on the board"))
    return number


def last_of_base(list_: list[Column], column: Column) -> bool:
    """A task of every state, set by the AI assistant too, needs a place on the board: the last column of its base
    is not deleted and keeps its base."""
    return not any(c.base == column.base and c.key != column.key for c in list_)


def keep_base(list_: list[Column], column: Column) -> None:
    """Raises ValueError if the column is the last one of its base."""
    if last_of_base(list_, column):
        raise ValueError(
            tr(
                "“{name}” is the only column counted as “{base}”: the board needs one. Add another such column first.",
                name=column_name(column),
                base=tr(BASES[column.base]),
            )
        )


@transcript.locked_edit(FILE)
def add_column(recordings: pathlib.Path, project: str, name: str, after: str) -> Column:
    """A new column to the right of the after column, with its base."""
    list_ = columns(recordings, project)
    number = _index(list_, after)
    column = Column(uuid.uuid4().hex[:10], list_[number].base, _name(list_, name))
    list_.insert(number + 1, column)
    set_columns(recordings, project, list_)
    return column


@transcript.locked_edit(FILE)
def rename_column(recordings: pathlib.Path, project: str, key: str, name: str) -> None:
    list_ = columns(recordings, project)
    number = _index(list_, key)
    list_[number] = dataclasses.replace(list_[number], name=_name(list_, name, key))
    set_columns(recordings, project, list_)


@transcript.locked_edit(FILE)
def order_columns(recordings: pathlib.Path, project: str, keys: list[str]) -> None:
    """keys are all the board's columns in the new order; ValueError if the board has changed meanwhile."""
    by_key = {c.key: c for c in columns(recordings, project)}
    if sorted(keys) != sorted(by_key):
        raise ValueError(tr("The columns have changed meanwhile, look at them again"))
    set_columns(recordings, project, [by_key[key] for key in keys])
