# SPDX-License-Identifier: GPL-3.0-only
"""Settings window: sections on the left, changes apply at once, there is no "Save" button (as usual in Windows);
a text field shows "Saved" after it is written.

What a setting does is in the tooltip on its name; a gray line under a setting appears only where it is easy
to go wrong without it. Each section is a page of its own module (settings_recognition, settings_minutes,
settings_recordings, settings_meetings, settings_program); the window links the two connections with the sections
that depend on them and shows the first-run tips.
"""

from PySide6.QtCore import QRect, Signal
from PySide6.QtGui import QCloseEvent, QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QStackedWidget, QVBoxLayout, QWidget

from knotes import VERSION
from knotes import settings
from knotes.language import tr
from knotes.ui import style, tour
from knotes.ui.connection import ConnectionBlock
from knotes.ui.settings_data import SECTIONS
from knotes.ui.settings_form import Editor
from knotes.ui.settings_meetings import MeetingsPage
from knotes.ui.settings_minutes import MinutesPage
from knotes.ui.settings_program import ProgramPage
from knotes.ui.settings_recognition import RecognitionPage
from knotes.ui.settings_recordings import RecordingsPage


def _ink() -> QColor:
    """The text color of the theme for icons: they are drawn again when the theme changes."""
    return QGuiApplication.palette().color(QPalette.ColorRole.WindowText)


class SettingsWindow(QWidget):
    """Signal changed(Settings) after each change; the API key is saved to the Windows credential store at once.
    restart(): the interface language changed and the user asks to restart the program now.
    check_updates(): the user asks to check for a new version. Shown in the meetings window instead of the list."""

    changed = Signal(object)
    restart = Signal()
    check_updates = Signal()

    def __init__(self, config: settings.Settings, parent_: QWidget | None = None):
        super().__init__(parent_)
        self.editor = Editor(config, self.changed.emit)
        self.connections: dict[str, ConnectionBlock] = {}
        for function, another, section in (
            ("распознавание", "протоколы", tr("Протоколы")),
            ("протоколы", "распознавание", tr("Speech recognition")),
        ):
            block = ConnectionBlock(
                function, self.settings, (section, lambda d=another: self._connection_address(d)), self
            )
            block.edited.connect(self._connection_changed)
            self.connections[function] = block
        self.connections["распознавание"].refresh_shared()
        self.recognition = RecognitionPage(self.editor, self.connections["распознавание"])
        self.minutes = MinutesPage(self.editor, self.connections["протоколы"])
        self.meetings = MeetingsPage(self.editor)
        self.program = ProgramPage(self.editor)
        self.program.restart.connect(self.restart)
        self.program.check_updates.connect(self.check_updates)
        self.sections, self.pages = QListWidget(), QStackedWidget()
        self.sections.setObjectName("разделы")
        self.sections.setFixedWidth(190)
        for code, sign, page in [
            ("распознавание", style.MICROPHONE, self.recognition),
            ("протоколы", style.DOCUMENT, self.minutes),
            ("записи", style.FOLDER, RecordingsPage(self.editor)),
            ("встречи", style.VIDEO, self.meetings),
            ("программа", style.SETTINGS, self.program),
        ]:
            self.editor.icons.append((QListWidgetItem(tr(SECTIONS[code]), self.sections), sign))
            self.pages.addWidget(page)
        self.sections.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.sections.setCurrentRow(0)
        self._build()
        self.repaint_theme()

    @property
    def settings(self) -> settings.Settings:
        return self.editor.settings

    def _build(self) -> None:
        middle = QHBoxLayout()  # sections on the window background, section settings as a card
        middle.setSpacing(8)
        middle.addWidget(self.sections)
        middle.addWidget(style.layer(self.pages, padding=2), 1)
        version = QLabel(f"{style.NAME} {VERSION}")
        version.setObjectName("тихий_текст")
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addLayout(middle, 1)
        column.addWidget(version)

    def repaint_theme(self) -> None:
        """The style and the icons drawn in the text color follow the theme."""
        self.setStyleSheet(style.window_style())
        for owner, sign in self.editor.icons:
            owner.setIcon(style.icon(sign, _ink()))  # type: ignore[attr-defined]

    def show_section(self, code: str) -> None:
        """A section by its code from SECTIONS, as if chosen on the left."""
        self.sections.setCurrentRow(list(SECTIONS).index(code))

    def _connection_address(self, function: str) -> str:
        """Connection address of a function; empty until its block is created."""
        block = self.connections.get(function)
        return block.connection.address if block else ""

    def _connection_changed(self, fields: dict) -> None:
        self.editor.change(**fields)
        for block in self.connections.values():
            block.refresh_shared()
        self.recognition.show_local_rows()

    def tips(self) -> list[tour.Step]:
        """Tips for the first opening: the preset list, then the key of the service or the model on this computer,
        then who writes the minutes."""
        speech, local_model = self.connections["распознавание"], self.recognition.local_model
        steps = [
            tour.Step(
                tr("How to recognize"),
                tr('Choose in this list. "On this computer" is free, no key needed.'),
                lambda: tour.area_of(speech.preset, self),
            )
        ]
        if speech.local_engine:
            steps.append(
                tour.Step(
                    tr("Model"),
                    local_model.tip(),
                    lambda: tour.area_of(local_model.download, self) or tour.area_of(local_model.switch_to, self),
                )
            )
        else:
            site = speech.connection.key_site
            steps.append(
                tour.Step(
                    tr("Service key"),
                    tr('The key is paid, you get it at {site}. Paste it and click "Check".', site=site)
                    if site
                    else tr('Paste the key and click "Check".'),
                    lambda: tour.area_of(speech.secret.field, self),
                )
            )
        steps.append(tour.Step(tr("Протоколы"), self.minutes.tip(), lambda: self._section_place(1)))
        return steps

    def _section_place(self, number: int) -> QRect | None:
        return tour.area_of(self.sections.viewport(), self, self.sections.visualItemRect(self.sections.item(number)))

    def show_tips(self) -> None:
        """Tips over the settings; when they end, the settings remember they were shown."""
        guide = tour.Tour(self, self.tips())
        guide.finished.connect(
            lambda _how: self.editor.change(
                tips_shown=[*(p for p in self.settings.tips_shown if p != "настройки"), "настройки"]
            )
        )
        guide.start()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Left the settings without leaving a field (Alt+Left, window closed): the unfinished field is saved too."""
        for block in self.connections.values():
            block.save_fields()
        self.minutes.save_name()
        self.meetings.calendar.save()
        super().closeEvent(event)
