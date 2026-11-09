# SPDX-License-Identifier: GPL-3.0-only
"""Shortcut fields of the "App" settings section: they work from any program, one shortcut does one thing."""

from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QFormLayout, QKeySequenceEdit, QLabel, QWidget

from knotes import system
from knotes.language import tr, tr_key
from knotes.ui import style
from knotes.ui.form import row, row_label
from knotes.ui.settings_data import HINTS
from knotes.ui.settings_form import Editor

HOTKEYS = {  # shortcut fields, what they do and the hint code
    "hotkey": (tr_key("Start and stop recording"), "клавиша"),
    "mark_hotkey": (tr_key("Add a mark"), "отметка"),
    "notes_hotkey": (tr_key("Notepad while recording"), "заметка"),
}


class HotkeyRows:
    """A field for each shortcut and a line under them: the shortcut set or why it was refused."""

    def __init__(self, editor: Editor):
        self._editor = editor
        self.result = QLabel(tr("Click the field and press the shortcut"))
        self.result.setObjectName("тихий_текст")

    def into_form(self, form_layout: QFormLayout) -> None:
        for field, (action, hint) in HOTKEYS.items():
            form_layout.addRow(row_label(tr(action), tr(HINTS[hint])), self._field(field))
        form_layout.addRow("", self.result)

    def _field(self, field: str) -> QWidget:
        edit = QKeySequenceEdit(QKeySequence(getattr(self._editor.settings, field)))
        edit.setMaximumSequenceLength(1)
        edit.setFixedWidth(180)
        edit.editingFinished.connect(lambda: self.set_hotkey(field, edit))
        disable = style.icon_button(
            style.CLOSE, tr("Turn off shortcut"), lambda: (edit.clear(), self.set_hotkey(field, edit))
        )
        return row(edit, disable)

    def set_hotkey(self, field: str, edit: QKeySequenceEdit) -> None:
        """A shortcut used by another of the three fields is refused: Windows would give it to one of them only."""
        config = self._editor.settings
        shortcut = edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        taken = next((f for f in HOTKEYS if f != field and shortcut and getattr(config, f) == shortcut), None)
        if shortcut and system.windows_shortcut(shortcut) is None:
            failure = tr("Use Ctrl, Alt, Shift or Win with a key, or F1-F24")
        elif taken is not None:
            failure = tr("{shortcut} is already used for “{action}”", shortcut=shortcut, action=tr(HOTKEYS[taken][0]))
        else:
            style.restyle(self.result, "тихий_текст")
            self.result.setText(tr("Shortcut: {shortcut}", shortcut=shortcut) if shortcut else tr("Shortcut off"))
            self._editor.change(**{field: shortcut})
            return
        self.result.setText(failure)
        style.restyle(self.result, "ошибка_текст")
        edit.setKeySequence(QKeySequence(getattr(config, field)))
