# SPDX-License-Identifier: GPL-3.0-only
"""Screen sharing: the recording notepad and its popups (project list, menus, tooltips) are hidden from participants."""

import pathlib
import tempfile
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from knotes import system
from knotes.ui import style
from knotes.ui.notepad import NotepadWindow

app = QApplication.instance() or QApplication([])


class TestHiding(unittest.TestCase):
    def test_notepad_project_list_hidden_from_sharing(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp) / "2026-10-23_10-00"
            folder.mkdir()
            filter_item = style.PopupHider()
            app.installEventFilter(filter_item)
            window = NotepadWindow(folder, True, ["Склад"], lambda: 0.0)
            try:
                window.show()
                window.activateWindow()
                app.processEvents()
                self.assertTrue(window.property(style.HIDDEN_FROM_CAPTURE))
                with (
                    mock.patch.object(system, "hide_from_capture") as hide_,
                    mock.patch.object(QApplication, "activeWindow", return_value=window),
                ):
                    window.project.showPopup()
                    app.processEvents()
                    popup = window.project.view().window()
                    hide_.assert_any_call(int(popup.winId()), True)
                    window.project.hidePopup()
            finally:
                app.removeEventFilter(filter_item)
                window.close()
                window.deleteLater()
                app.processEvents()


if __name__ == "__main__":
    unittest.main()
