# SPDX-License-Identifier: GPL-3.0-only
"""Writing to the task board from its windows: a failure goes to the log and to a clear message, and the caller keeps
what the user typed."""

import logging
import pathlib
from collections.abc import Callable

from PySide6.QtWidgets import QWidget

from knotes import failures, language, tasks
from knotes.ui import style

log = logging.getLogger(__name__)


def failure_text(error: Exception) -> str:
    """The board's own refusals are already a clear text; the rest goes through failures."""
    return str(error) if isinstance(error, ValueError) and not isinstance(error, UnicodeError) else failures.text(error)


def safely(parent_: QWidget, action: Callable[[], object]) -> bool:
    """Writes to the board; a failure goes to the log and to a clear message, and the caller keeps what the user
    typed. True if written."""
    try:
        action()
    except (OSError, ValueError) as error:
        log.warning("the board change was not saved: %s", error)
        style.ask(parent_, language.tr("Tasks"), failure_text(error), warning=True)
        return False
    return True


def delete_task(parent_: QWidget, recordings: pathlib.Path, task: tasks.Task) -> bool:
    """Deletes an own task after confirmation; True if it was deleted."""
    if not style.ask(parent_, language.tr("Delete task"), language.tr("Delete task “{text}”?", text=task.text)):
        return False
    return safely(parent_, lambda: tasks.delete(recordings, task.key))
