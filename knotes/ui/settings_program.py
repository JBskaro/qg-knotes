# SPDX-License-Identifier: GPL-3.0-only
"""The "App" settings section: start with Windows, updates, the AI assistant, shortcuts, hiding the bar from screen
sharing, the theme and the interface language."""

from PySide6.QtCore import Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QLabel, QPushButton, QWidget

from knotes import ai_tools, language, system
from knotes.language import tr, tr_key
from knotes.ui import form, style
from knotes.ui.form import AS_IN_WINDOWS, column, row, row_label
from knotes.ui.settings_data import HINTS
from knotes.ui.settings_form import Editor, Page, labels_text
from knotes.ui.settings_hotkeys import HotkeyRows

THEMES = [("система", AS_IN_WINDOWS), ("светлая", tr_key("Светлое")), ("темная", tr_key("Темное"))]
UI_LANGUAGES = language.LANGUAGES  # in their own names: anyone finds their language without reading the current one


class ProgramPage(Page):
    """Signal restart(): the interface language changed and the user asks to restart the program now."""

    restart = Signal()

    def __init__(self, editor: Editor):
        super().__init__(editor)
        form_layout = self.form_layout
        autostart = style.Switch(system.autostart_enabled())
        self.autostart_result = QLabel()
        self.autostart_result.setObjectName("ошибка_текст")
        self.autostart_result.setWordWrap(True)
        self.autostart_result.hide()
        autostart.toggled.connect(lambda on: self._set_autostart(autostart, on))
        form_layout.addRow(
            row_label(tr("Start with Windows"), tr(HINTS["автозапуск"])),
            column(row(autostart), self.autostart_result),
        )
        form_layout.addRow(
            row_label(tr("Проверять обновления"), tr(HINTS["обновления"])), row(editor.switch("check_for_updates"))
        )
        form_layout.addRow(row_label(tr("AI assistant (MCP)"), tr(HINTS["ии"])), self._ai_connection())
        self.hotkeys = HotkeyRows(editor)
        self.hotkeys.into_form(form_layout)
        form_layout.addRow(
            row_label(tr("Hide the bar when sharing screen"), tr(HINTS["скрывать"])), row(editor.switch("hide_bar"))
        )
        form_layout.addRow(row_label(tr("Theme"), tr(HINTS["тема"])), editor.segments(labels_text(THEMES), "theme"))
        languages = [("", tr(AS_IN_WINDOWS)), *UI_LANGUAGES.items()]
        self.ui_language = editor.choice(languages, "ui_language")
        self.ui_language.currentIndexChanged.connect(self._show_restart)
        self.restart_now = QPushButton(tr("Restart now"))
        self.restart_now.clicked.connect(self.restart.emit)
        caption = QLabel(tr("The language changes after the app restarts"))
        caption.setObjectName("тихий_текст")
        self.restart_row = row(caption, self.restart_now)
        form_layout.addRow(row_label(tr("Interface language"), tr(HINTS["язык_интерфейса"])), row(self.ui_language))
        form_layout.addRow("", self.restart_row)
        self._show_restart()

    def _set_autostart(self, switch: style.Switch, on: bool) -> None:
        """The registry entry; if Windows refused it, the switch goes back and says why."""
        written = form.stored(lambda: system.set_autostart(on), "autostart")
        self.autostart_result.setText(
            tr("Windows did not let the app change the autostart. Details are in the app log.")
        )
        self.autostart_result.setVisible(not written)
        if not written:
            switch.blockSignals(True)
            switch.setChecked(not on)
            switch.blockSignals(False)

    def _show_restart(self) -> None:
        """The restart button, only when the chosen language is not the one the program runs in now."""
        self.restart_row.setVisible(language.which(self.editor.settings.ui_language) != language.current())

    @staticmethod
    def _ai_connection() -> QWidget:
        """The button copies the MCP config with the path to this program; next to it, what to do with it."""
        result = QLabel()
        result.setObjectName("тихий_текст")
        button = QPushButton(tr("Copy config for Claude"))

        def copy() -> None:
            QGuiApplication.clipboard().setText(ai_tools.client_config())
            result.setText(tr("Copied: paste it into the Claude Desktop config and restart it"))

        button.clicked.connect(copy)
        return row(button, result)
