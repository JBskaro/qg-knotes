# SPDX-License-Identifier: GPL-3.0-only
"""The "Speech recognition" settings section: the connection or the model on this computer, the transcript during
the meeting, the meeting language and the vocabulary."""

from PySide6.QtWidgets import QLabel

from knotes import live, vocabulary
from knotes.language import tr, tr_key
from knotes.ui import style
from knotes.ui.connection import ConnectionBlock
from knotes.ui.form import column, row, row_label
from knotes.ui.settings_data import HINTS
from knotes.ui.settings_form import Editor, Page, labels_text
from knotes.ui.settings_local_model import LocalModelRows
from knotes.ui.vocabulary_window import VocabularyDialog

LANGUAGES = [("ru", tr_key("Русский")), ("en", tr_key("Английский")), ("", tr_key("Определять автоматически"))]


class RecognitionPage(Page):
    def __init__(self, editor: Editor, speech: ConnectionBlock):
        super().__init__(editor)
        self._speech = speech
        speech.into_form(self.form_layout)
        self.local_model = LocalModelRows(editor, speech, self)
        self.local_model.into_form(self.form_layout)
        self.live_switch, self.live_note = editor.switch("live_transcript"), QLabel(tr(live.CLOUD))
        self.live_note.setObjectName("тихий_текст")
        self.live_note.setWordWrap(True)
        self.form_layout.addRow(
            row_label(tr("Transcript during the meeting"), tr(live.HINT)),
            column(row(self.live_switch), self.live_note),
        )
        self.show_local_rows()
        self.form_layout.addRow(
            row_label(tr("Meeting language"), tr(HINTS["язык"])),
            row(editor.choice(labels_text(LANGUAGES), "language")),
        )
        self.vocabulary = editor.with_icon(style.BOOK)
        self.vocabulary.clicked.connect(self.open_vocabulary)
        self.form_layout.addRow(row_label(tr("Vocabulary"), tr(HINTS["словарь"])), row(self.vocabulary))
        self._label_vocabulary()

    def show_local_rows(self) -> None:
        """Model and diarization rows only for speech recognition on this computer; the transcript during the meeting
        too: the cloud recognizes after the meeting."""
        local = self._speech.local_engine
        for line in self.local_model.rows:
            self.form_layout.setRowVisible(line, local)
        self.live_switch.setEnabled(local)
        self.live_note.setVisible(not local)
        self.local_model.refresh()

    def _label_vocabulary(self) -> None:
        count = len(self.editor.settings.vocabulary)
        self.vocabulary.setText("  " + (tr("Vocabulary, words: {n}", n=count) if count else tr("Vocabulary is empty")))

    def open_vocabulary(self) -> None:
        """The words are kept however the window is closed: "Done", the cross or Esc."""
        window = VocabularyDialog(vocabulary.from_data(self.editor.settings.vocabulary), self)
        window.exec()
        words = vocabulary.to_data(window.words())
        if words != self.editor.settings.vocabulary:
            self.editor.change(vocabulary=words)
            self._label_vocabulary()
