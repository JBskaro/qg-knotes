# SPDX-License-Identifier: GPL-3.0-only
"""Writing to the task board from its windows: a failure goes to the log and to a clear message, and the caller keeps
what the user typed."""

import logging
import pathlib
from collections.abc import Callable

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QPlainTextEdit, QWidget

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


def from_selection(
    parent: QWidget, recordings: pathlib.Path, text: str, project: str, source: tasks.Source | None = None
) -> bool:
    dialog = QDialog(parent)
    dialog.setWindowTitle(language.tr("Create task…"))
    form = QFormLayout(dialog)
    title = QPlainTextEdit(text.replace("\u2029", "\n"))
    who = QLineEdit()
    form.addRow(language.tr("What needs to be done"), title)
    form.addRow(language.tr("Исполнитель"), who)
    form.addRow(language.tr("Project"), QLineEdit(project, readOnly=True))
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
    create = buttons.addButton(language.tr("Create task"), QDialogButtonBox.ButtonRole.AcceptRole)
    create.setEnabled(bool(title.toPlainText().strip()))
    title.textChanged.connect(lambda: create.setEnabled(bool(title.toPlainText().strip())))

    def submit() -> None:
        if safely(
            dialog,
            lambda: tasks.add(
                recordings, title.toPlainText().strip(), project, tasks.Edit(who=who.text().strip()), source
            ),
        ):
            dialog.accept()

    buttons.accepted.connect(submit)
    buttons.rejected.connect(dialog.reject)
    form.addRow(buttons)
    return dialog.exec() == QDialog.DialogCode.Accepted


def delete_task(parent_: QWidget, recordings: pathlib.Path, task: tasks.Task) -> bool:
    """Delete a card after confirmation, keeping the original minutes item."""
    message = language.tr("Delete task “{text}”?", text=task.text)
    if not task.custom:
        message += "\n\n" + language.tr("The item will stay in the minutes.")
    if not style.confirm(parent_, language.tr("Delete task"), message, language.tr("Delete")):
        return False
    return safely(parent_, lambda: tasks.delete(recordings, task.key))
