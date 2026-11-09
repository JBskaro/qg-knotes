# SPDX-License-Identifier: GPL-3.0-only
"""Saving the minutes or the transcript as a file: where to, and PDF or Word by the type chosen in the dialog."""

import logging
import pathlib

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QWidget

from knotes import export
from knotes.language import tr

log = logging.getLogger(__name__)


def save_as(parent: QWidget, document: export.Document, name: str) -> str:
    """Asks where to save; the type chosen in the dialog, or the extension typed, decides PDF or DOCX. Returns what
    to tell the user; empty when they changed their mind or the failure was already shown."""
    pdf, word = f"{tr('PDF document')} (*.pdf)", f"{tr('Word document')} (*.docx)"
    folder = pathlib.Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation))
    path, chosen = QFileDialog.getSaveFileName(
        parent, tr("Save as file"), str(folder / f"{name}{export.PDF}"), f"{pdf};;{word}"
    )
    if not path:
        return ""
    target = export.with_extension(pathlib.Path(path), export.DOCX if chosen == word else export.PDF)
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)  # a long transcript takes a second or two
    try:
        export.save(document, target)
    except OSError as error:
        log.warning("file not saved: %s", error)
        problem = export.failure_text(error, target)
    else:
        problem = ""
    finally:
        QApplication.restoreOverrideCursor()
    if problem:
        QMessageBox.warning(parent, tr("Save as file"), problem)
        return ""
    return tr("Saved: {file}", file=target.name)
