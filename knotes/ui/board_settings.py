# SPDX-License-Identifier: GPL-3.0-only
"""The board settings window, opened by "Set up the board" on any board: the Columns tab (the columns of a project)
and the Labels tab (the labels of all projects). Every change is written at once; Close only closes."""

import pathlib
from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QStackedWidget, QVBoxLayout, QWidget

from knotes import language
from knotes.ui import style
from knotes.ui.board_columns_page import ColumnsPage
from knotes.ui.board_labels import LabelsPage
from knotes.ui.board_settings_page import dialog_button

COLUMNS, LABELS = "колонки", "метки"


class BoardSettings(QDialog):
    """project: the project whose columns the Columns tab shows first. The changed signal comes after every written
    change: the board builds itself again."""

    changed = Signal()

    def __init__(
        self,
        parent_: QWidget,
        recordings: pathlib.Path,
        projects_: tuple[Sequence[str], dict[str, dict]],
        project: str,
        tab: str = COLUMNS,
    ):
        super().__init__(parent_)
        self.setWindowTitle(language.tr("Board settings"))
        self.columns = ColumnsPage(recordings, projects_, project)
        self.labels = LabelsPage(recordings)
        self.pages = QStackedWidget()
        for page in (self.columns, self.labels):
            page.changed.connect(self.changed)
            self.pages.addWidget(page)
        self.tabs = style.Segments([(COLUMNS, language.tr("Columns")), (LABELS, language.tr("Labels"))], tab)
        self.tabs.selected.connect(self.show_tab)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(self.tabs)
        layout.addWidget(self.pages, 1)
        layout.addWidget(dialog_button(language.tr("Close"), self.accept), 0, Qt.AlignmentFlag.AlignRight)
        self.setStyleSheet(style.window_style())
        self.show_tab(tab)

    def show_tab(self, tab: object) -> None:
        self.tabs.select(tab)
        self.pages.setCurrentWidget(self.columns if tab == COLUMNS else self.labels)
