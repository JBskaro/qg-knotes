# SPDX-License-Identifier: GPL-3.0-only
"""Renaming and deleting a project: its meetings, its notes and own tasks go to the new name together or not at all.

The files the change writes are read first; a write that fails halfway (a full disk, a file held by another program)
puts back the ones already written, so no meeting is left under the new name while the notes stay under the old one.
"""

import logging
import pathlib
from collections.abc import Callable

from knotes import catalog, failures, projects, tasks, transcript
from knotes.language import tr

log = logging.getLogger(__name__)
UNREAD = object()  # a written file that could not be read back: it is not put back blindly


class NotRenamed(Exception):
    """The project keeps its name. left: files that could not be put back as they were."""

    def __init__(self, error: OSError | ValueError, left: list[pathlib.Path]):
        super().__init__(str(error))
        self.error, self.left = error, left

    def text(self) -> str:
        """Why, in words, and whether everything is as before."""
        reason = failures.text(self.error)
        if not self.left:
            return tr("{reason} Nothing has changed.", reason=reason)
        files = ", ".join(str(pathlib.Path(p.parent.name, p.name)) for p in self.left)
        return tr("{reason} These files were not put back as they were: {files}.", reason=reason, files=files)


def rename(recordings: pathlib.Path | None, meetings: list[catalog.Meeting], old_value: str, new_value: str) -> None:
    """The meetings of the project get the new name, its notes and own tasks follow it; an empty new name deletes the
    project: its meetings stay without a project, its notes go, its tasks are left without a project. Raises
    NotRenamed when it did not go through: what was written by then is put back."""
    folders = [v.folder for v in meetings if v.project == old_value and v.folder.is_dir()]
    steps: list[tuple[pathlib.Path, Callable[[], None]]] = [
        (folder / catalog.PROPERTIES, lambda f=folder: catalog.to_project([f], new_value)) for folder in folders
    ]
    if recordings is not None:
        steps += [
            (recordings / projects.FILE, lambda: projects.rename(recordings, old_value, new_value)),
            (recordings / tasks.FILE, lambda: tasks.rename_project(recordings, old_value, new_value)),
        ]
    try:
        before = {path: _content(path) for path, _ in steps}  # nothing is written if one can't be read
    except OSError as error:
        raise _not_renamed(old_value, new_value, error, []) from error
    written: dict[pathlib.Path, bytes | None | object] = {}  # a file as this rename left it
    for path, step in steps:
        try:
            step()
        except (OSError, ValueError) as error:  # ValueError: a file not in UTF-8
            raise _not_renamed(old_value, new_value, error, _put_back(before, written)) from error
        try:
            written[path] = _content(path)
        except OSError:
            written[path] = UNREAD


def _not_renamed(old_value: str, new_value: str, error: OSError | ValueError, left: list[pathlib.Path]) -> NotRenamed:
    log.warning("project %r not renamed to %r: %s; not put back: %s", old_value, new_value, error, left)
    return NotRenamed(error, left)


def _content(path: pathlib.Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _put_back(before: dict[pathlib.Path, bytes | None], written: dict[pathlib.Path, object]) -> list[pathlib.Path]:
    """The files this rename wrote, as they were; the ones that could not be put back. A file another program
    (the MCP server) changed since is not written over: its change stays, the file is among those not put back. The
    file whose write failed was not written: it is left alone too."""
    left = []
    for path, after in written.items():
        if after == before[path]:  # this step changed nothing in it
            continue
        try:
            if _content(path) != after:
                log.warning("file %s changed by another program, not put back after a failed project rename", path)
                left.append(path)
            else:
                _write_back(path, before[path])
        except OSError as error:
            log.warning("file %s not put back after a failed project rename: %s", path, error)
            left.append(path)
    return left


def _write_back(path: pathlib.Path, content: bytes | None) -> None:
    """Through a temporary file, as transcript.write_text: a cut-off write leaves the file whole."""
    if content is None:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(f"{path.name}.back.tmp")
    try:
        temporary.write_bytes(content)
        transcript.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
