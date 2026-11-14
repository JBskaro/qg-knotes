# SPDX-License-Identifier: GPL-3.0-only
"""Local notes that can be created without a meeting."""

import dataclasses
import datetime
import pathlib
import uuid

from knotes import transcript
from knotes.language import tr

FILE = pathlib.Path(".узелок", "блокнот.json")
VIEW_FILE = FILE.with_name("блокнот-вид.json")


@dataclasses.dataclass(frozen=True)
class Note:
    id: str = ""
    title: str = ""
    text: str = ""
    project: str = ""
    updated: str = ""
    pinned: bool = False
    archived: bool = False
    rich_text: str = ""


@dataclasses.dataclass(frozen=True)
class View:
    project: str | None = None
    note_id: str = ""
    position: int = 0
    archived: bool = False


def view(recordings: pathlib.Path) -> View:
    data = transcript.read_json(recordings / VIEW_FILE, {})
    project, note_id, position = data.get("проект"), data.get("заметка"), data.get("позиция")
    return View(
        project if isinstance(project, str) else None,
        note_id if isinstance(note_id, str) else "",
        position if type(position) is int and position > 0 else 0,
        data.get("архив") is True,
    )


@transcript.locked_edit(VIEW_FILE)
def remember_view(recordings: pathlib.Path, state: View) -> None:
    if state == view(recordings):
        return
    transcript.write_json(
        recordings / VIEW_FILE,
        {"проект": state.project, "заметка": state.note_id, "позиция": state.position, "архив": state.archived},
    )


def _data(recordings: pathlib.Path) -> dict:
    return transcript.read_json(recordings / FILE, {})


def _note(key: str, data: dict) -> Note:
    return Note(
        key,
        data.get("название", ""),
        data.get("текст", ""),
        data.get("проект", ""),
        data.get("обновлена", ""),
        data.get("закреплена") is True,
        data.get("архив") is True,
        data.get("оформление", ""),
    )


def list_(recordings: pathlib.Path) -> list[Note]:
    items = (_note(key, item) for key, item in _data(recordings).items() if isinstance(item, dict))
    return sorted(items, key=lambda note: (note.pinned, note.updated), reverse=True)


@transcript.locked_edit(FILE)
def save(recordings: pathlib.Path, note: Note, previous: Note) -> Note:
    items = _data(recordings)
    if previous.id:
        _check_current(items, previous)
    saved = dataclasses.replace(
        note,
        id=previous.id or uuid.uuid4().hex,
        title=note.title.strip(),
        updated=datetime.datetime.now(datetime.UTC).isoformat(),
    )
    items[saved.id] = {
        "название": saved.title,
        "текст": saved.text,
        "проект": saved.project,
        "обновлена": saved.updated,
        "закреплена": saved.pinned,
        "архив": saved.archived,
    }
    if saved.rich_text:
        items[saved.id]["оформление"] = saved.rich_text
    transcript.write_json(recordings / FILE, items)
    return saved


def _check_current(items: dict, note: Note) -> None:
    if note.id not in items or _note(note.id, items[note.id]) != note:
        raise OSError(tr("The note changed in another window. Your changes are still in the editor."))


@transcript.locked_edit(FILE)
def delete(recordings: pathlib.Path, note: Note) -> None:
    items = _data(recordings)
    _check_current(items, note)
    del items[note.id]
    transcript.write_json(recordings / FILE, items)


@transcript.locked_edit(FILE)
def rename_project(recordings: pathlib.Path, old: str, new: str) -> None:
    items = _data(recordings)
    matching = [item for item in items.values() if isinstance(item, dict) and item.get("проект") == old]
    if matching:
        for item in matching:
            item["проект"] = new
        transcript.write_json(recordings / FILE, items)
