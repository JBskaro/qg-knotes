# SPDX-License-Identifier: GPL-3.0-only
"""The "Minutes" settings section: the connection of the model that writes the minutes, your name instead of "Me",
the instructions and hiding personal data from a cloud model."""

from PySide6.QtWidgets import QLabel, QLineEdit

from knotes import minutes
from knotes.language import tr
from knotes.ui import privacy_rows, style
from knotes.ui.connection import ConnectionBlock
from knotes.ui.form import SavedMark, row, row_label
from knotes.ui.minutes_view import InstructionDialog
from knotes.ui.settings_data import HINTS
from knotes.ui.settings_form import Editor, Page


class MinutesPage(Page):
    def __init__(self, editor: Editor, service: ConnectionBlock):
        super().__init__(editor)
        self._service = service
        service.into_form(self.form_layout)
        self.about_assistant = QLabel(
            tr(
                "Your own AI assistant (Claude Desktop and others) can write the minutes too, with its own model, next "
                'to this connection. Connect it in the "App" section.'
            )
        )
        self.about_assistant.setObjectName("тихий_текст")
        self.about_assistant.setWordWrap(True)
        self.form_layout.addRow("", self.about_assistant)
        self.name = QLineEdit(editor.settings.my_name)
        self.name.setPlaceholderText(tr("Как назвать вас вместо «Я»"))
        self.name.editingFinished.connect(self.save_name)
        self.name_saved = SavedMark()
        self._instruction = editor.with_icon(style.DOCUMENT)
        self._instruction.clicked.connect(self._open_instruction)
        self.form_layout.addRow(
            row_label(tr("Your name"), tr(HINTS["имя"])), row(self.name, self.name_saved, stretch=True)
        )
        self.form_layout.addRow(row_label(tr("Instructions"), tr(HINTS["инструкция"])), row(self._instruction))
        privacy_rows.add(self.form_layout, editor.switch)
        self._label_instruction()

    def tip(self) -> str:
        """Who writes the minutes, for the first-run tips."""
        if not self._service.connection.key_needed:
            return tr("AI on your server writes the decisions and tasks.")
        if self._service.shared_key:
            return tr("AI writes the decisions and tasks. The key is shared with speech recognition.")
        site = self._service.connection.key_site
        return (
            tr(
                "AI writes the decisions and tasks with a paid key, for example from {site}. Without a key you get "
                "only the transcript.",
                site=site,
            )
            if site
            else tr("AI writes the decisions and tasks with a key. Without a key you get only the transcript.")
        )

    def save_name(self) -> None:
        name = self.name.text().strip()
        if name != self.editor.settings.my_name:
            self.editor.change(my_name=name)
            self.name_saved.display()

    def _label_instruction(self) -> None:
        custom = minutes.instruction_file().exists()
        self._instruction.setText("  " + (tr("Custom instructions") if custom else tr("Default instructions")))

    def _open_instruction(self) -> None:
        InstructionDialog(self).exec()
        self._label_instruction()
