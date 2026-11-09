# SPDX-License-Identifier: GPL-3.0-only
"""The "Recordings" settings section: the folder, what to do after recording, where the transcript opens, screen
recording, when audio is deleted, whether providers may keep it."""

import pathlib

from PySide6.QtWidgets import QFileDialog, QFormLayout, QLabel, QPushButton

from knotes import screen
from knotes.language import tr, tr_key
from knotes.ui.form import AS_IN_WINDOWS, row, row_label
from knotes.ui.settings_data import HINTS
from knotes.ui.settings_form import Editor, Page, labels_text

AFTER_RECORDING = [
    ("спросить", tr_key("Спрашивать, отправлять ли")),
    ("сразу", tr_key("Отправлять сразу")),
    ("нет", tr_key("Только сохранить")),
]
OPEN_IN = [("программе", tr_key("В программе")), ("блокноте", tr_key("В Блокноте")), ("по_умолчанию", AS_IN_WINDOWS)]
QUALITY_OPTIONS = [("эконом", tr_key("Экономное")), ("обычное", tr_key("Обычное")), ("высокое", tr_key("Высокое"))]
DELETE_AUDIO_OPTIONS = [
    (-1, tr_key("Не удалять")),
    (0, tr_key("Сразу после расшифровки")),
    (30, tr_key("Через 30 дней")),
    (90, tr_key("Через 90 дней")),
]
RETENTION = {
    False: tr_key("Выключено: через OpenRouter звук не уходит провайдерам, которые хранят его после распознавания."),
    True: tr_key("Включено: звук может уйти и к провайдерам, которые его хранят."),
}


def _what_to_record() -> list[tuple[str, str]]:
    """What to record: the meeting window, the monitor with it, the primary monitor, other monitors by number."""
    others = [m for m in screen.monitors() if not m.primary]
    return [
        (screen.MEETING_WINDOW, tr_key("Окно встречи")),
        (screen.MEETING_SCREEN, tr_key("Экран с окном встречи")),
        (screen.PRIMARY, tr_key("Основной монитор")),
        *((m.device, tr("Monitor {n}", n=number)) for number, m in enumerate(others, 2)),
    ]


class RecordingsPage(Page):
    def __init__(self, editor: Editor):
        super().__init__(editor)
        form_layout = self.form_layout
        self._folder = QLabel()
        change = QPushButton(tr("Change"))
        change.clicked.connect(self._pick_folder)
        self._show_folder()
        form_layout.addRow(row_label(tr("Recordings folder"), tr(HINTS["папка"])), row(self._folder, change))
        form_layout.addRow(
            row_label(tr("After recording"), tr(HINTS["после"])),
            row(editor.choice(labels_text(AFTER_RECORDING), "after_recording")),
        )
        form_layout.addRow(
            row_label(tr("Открывать расшифровку"), tr(HINTS["открывать"])),
            editor.segments(labels_text(OPEN_IN), "open_in"),
        )
        self._screen_recording(form_layout)
        form_layout.addRow(
            row_label(tr("Delete audio and video"), tr(HINTS["удалять"])),
            row(editor.choice(labels_text(DELETE_AUDIO_OPTIONS), "delete_audio_after")),
        )
        retention = editor.switch("provider_retention")
        hint = QLabel(tr(RETENTION[editor.settings.provider_retention]))
        hint.setObjectName("тихий_текст")
        retention.toggled.connect(lambda on: hint.setText(tr(RETENTION[on])))
        form_layout.addRow(row_label(tr("Let providers keep audio"), tr(HINTS["хранение"])), row(retention))
        form_layout.addRow("", hint)

    def _screen_recording(self, form_layout: QFormLayout) -> None:
        """The "Record the screen" switch and, only when it is on, what to record and the quality under it."""
        screen_capture = self.editor.switch("record_screen")
        lines = [
            row(self.editor.choice(labels_text(_what_to_record()), "screen_capture")),
            self.editor.segments(labels_text(QUALITY_OPTIONS), "video_quality"),
        ]
        form_layout.addRow(row_label(tr("Record the screen"), tr(HINTS["экран"])), row(screen_capture))
        for row_label_, key_, line in zip(
            (tr("What to record"), tr("Quality")), ("что_снимать", "качество"), lines, strict=True
        ):
            form_layout.addRow(row_label(row_label_, tr(HINTS[key_])), line)
            form_layout.setRowVisible(line, self.editor.settings.record_screen)
            screen_capture.toggled.connect(lambda on, s=line: form_layout.setRowVisible(s, on))

    def _show_folder(self) -> None:
        path = pathlib.Path(self.editor.settings.recordings_folder)
        parts = path.parts
        self._folder.setText(str(path) if len(parts) <= 4 else "…\\" + "\\".join(parts[-3:]))
        self._folder.setToolTip(str(path))

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, tr("Folder for meeting recordings"), self.editor.settings.recordings_folder
        )
        if folder:
            self.editor.change(recordings_folder=str(pathlib.Path(folder)))
            self._show_folder()
