# SPDX-License-Identifier: GPL-3.0-only
"""The "Meetings" settings section: the microphone and the audio of the others, suggesting recording when a meeting
app turns on the microphone, the meeting apps watched, the calendar."""

import pathlib

from PySide6.QtWidgets import QCheckBox, QComboBox, QFileDialog, QVBoxLayout, QWidget

from knotes import recording, settings
from knotes.language import tr
from knotes.ui import form, style
from knotes.ui.form import row, row_label
from knotes.ui.settings_calendar import CalendarField
from knotes.ui.settings_data import HINTS
from knotes.ui.settings_form import Editor, Page


class MeetingsPage(Page):
    def __init__(self, editor: Editor):
        super().__init__(editor)
        form_layout = self.form_layout
        try:
            microphones, outputs = recording.device_list()
        except OSError:  # Windows audio service unavailable: only "Windows default" can be chosen
            microphones, outputs = [], []
        form_layout.addRow(
            row_label(tr("Microphone"), tr(HINTS["микрофон"])), row(self._device("microphone", microphones))
        )
        form_layout.addRow(
            row_label(tr("Other participants' audio"), tr(HINTS["вывод"])), row(self._device("output", outputs))
        )
        form_layout.addRow(
            row_label(tr("Suggest recording"), tr(HINTS["предлагать"])), row(editor.switch("suggest_recording"))
        )
        self._programs = QVBoxLayout()
        self._programs.setSpacing(6)
        list_ = QWidget()
        list_.setLayout(self._programs)
        self._checkboxes: list[tuple[QCheckBox, tuple[str, ...]]] = []
        for row_label_, _, processes in settings.MEETING_PROGRAMS:
            self._program_checkbox(tr(row_label_), processes)
        known = {p.lower() for *_, processes in settings.MEETING_PROGRAMS for p in processes}
        for process_ in editor.settings.meeting_apps:
            if process_.lower() not in known:
                self._program_checkbox(process_, (process_,))
        add = editor.with_icon(style.PLUS, "  " + tr("Add app"))
        add.clicked.connect(self._add_program)
        self._programs.addWidget(row(add))
        form_layout.addRow(row_label(tr("Meeting apps"), tr(HINTS["программы"])), list_)
        self.calendar = CalendarField()
        form_layout.addRow(row_label(tr("Calendar"), tr(HINTS["календарь"])), self.calendar)

    def _device(self, field: str, names: list[str]) -> QComboBox:
        list_ = form.devices(names, getattr(self.editor.settings, field))
        list_.setMinimumWidth(340)
        list_.currentIndexChanged.connect(lambda: self.editor.change(**{field: list_.currentData()}))
        return list_

    def _program_checkbox(self, row_label_: str, processes: tuple[str, ...]) -> None:
        checkbox = QCheckBox(row_label_)
        selected_items = {p.lower() for p in self.editor.settings.meeting_apps}
        checkbox.setChecked(any(p.lower() in selected_items for p in processes))
        checkbox.toggled.connect(self._collect_programs)
        self._checkboxes.append((checkbox, processes))
        self._programs.insertWidget(len(self._checkboxes) - 1, checkbox)

    def _collect_programs(self) -> None:
        self.editor.change(
            meeting_apps=[p for checkbox, processes in self._checkboxes if checkbox.isChecked() for p in processes]
        )

    def _add_program(self) -> None:
        file, _ = QFileDialog.getOpenFileName(self, tr("Meeting app"), "C:\\Program Files", tr("Programs (*.exe)"))
        if file:
            name = pathlib.Path(file).name
            self._program_checkbox(name, (name,))
            self._collect_programs()
