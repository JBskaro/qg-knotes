# SPDX-License-Identifier: GPL-3.0-only
"""Rows of speech recognition on this computer in the settings: the model with "Download" and "Choose a model…"
(the model window) and telling speakers apart, whose models download as soon as it is turned on."""

import logging

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QFormLayout, QLabel, QPushButton, QWidget

from knotes import failures, models, settings
from knotes.language import tr
from knotes.ui import style
from knotes.ui.connection import ConnectionBlock
from knotes.ui.form import column, row, row_label
from knotes.ui.models_window import ModelsWindow, description
from knotes.ui.settings_data import HINTS
from knotes.ui.settings_form import Editor
from knotes.ui.setup_wizard import LocalModel, ModelDownload, download, local_model

log = logging.getLogger(__name__)


class LocalModelRows(QObject):
    """The two rows, shown by the section only for the "On this computer" preset. A QObject: a download that outlives
    the settings stops telling it when the settings are gone."""

    def __init__(self, editor: Editor, speech: ConnectionBlock, page: QWidget):
        super().__init__(page)
        self._editor, self._speech, self._page = editor, speech, page
        self._models_window: ModelsWindow | None = None  # one per settings window: a started download shows again
        self._diarization_job: ModelDownload | None = None
        self.model_label = QLabel()
        self.model_label.setTextFormat(Qt.TextFormat.PlainText)  # the model name comes from Hugging Face
        self.download = QPushButton(tr("Download"))
        self.download.clicked.connect(lambda: self.pick_model(download=True))
        self.switch_to = QPushButton(tr("Choose a model…"))
        self.switch_to.clicked.connect(self.pick_model)
        diarization = editor.switch("speaker_diarization")
        diarization.toggled.connect(self.enable_diarization)
        self.diarization_status = QLabel()
        self.diarization_status.setWordWrap(True)
        self.diarization_status.hide()
        self.rows = [
            row(self.model_label, self.download, self.switch_to),
            column(row(diarization), self.diarization_status),
        ]

    def into_form(self, form_layout: QFormLayout) -> None:
        for row_label_, key_, line in zip(
            (tr("Model"), tr("Tell speakers apart")), ("модели", "разметка"), self.rows, strict=True
        ):
            form_layout.addRow(row_label(row_label_, tr(HINTS[key_])), line)

    def state(self) -> LocalModel:
        return local_model(self._speech.chosen_model, self._editor.settings.speaker_diarization)

    def refresh(self) -> None:
        state = self.state()
        if state.model is None:
            self.model_label.setText(tr("No model selected"))
            self.download.hide()
            return
        downloaded = state.model not in state.missing
        size = models.size_on_disk(settings.models_folder(), state.model)
        details_text = description(state.model, size) if downloaded else tr("not downloaded")
        self.model_label.setText(f"{tr(state.model.title)} · {details_text}")
        self.download.setVisible(not downloaded)

    def tip(self) -> str:
        """The same model state and size as in the first-run setup: with the speaker models when they are on."""
        state = self.state()
        if state.model is None:
            return tr('Click "Choose a model…".')
        if state.downloaded:
            return tr('The model is downloaded. Choose another one here: "Choose a model…".')
        size = state.size
        if not size:
            return tr('The model is not downloaded. Click "Download".')
        return tr(
            'The model is not downloaded. Click "Download", {size} once.',
            size=models.size_text(size).replace(" ", " "),
        )  # number and unit on one line

    def enable_diarization(self, on: bool) -> None:
        """Turned on: missing speaker diarization models (about 32 MB) download at once, with progress next to the
        switch; then a meeting is recognized without internet."""
        folder = settings.models_folder()
        need = [m for m in models.service_items(folder, True) if models.status(folder, m) != models.DOWNLOADED]
        if not on or not need or not self._speech.local_engine:
            return
        self._show_diarization_progress(0, 0)
        if self._diarization_job is not None:
            self._diarization_job.unfollow(self, self._show_diarization_progress, self._diarization_finished)
        # the download outlives the settings: leaving them does not cut it off
        self._diarization_job = job = download(need, tr("the models for telling speakers apart"))
        job.follow(self, self._show_diarization_progress, self._diarization_finished)
        if not job.running:
            job.start(folder, need, self._editor.settings.models_mirror)

    def _show_diarization_progress(self, downloaded_count: int, total: int) -> None:
        style.restyle(self.diarization_status, "тихий_текст")
        self.diarization_status.setText(
            tr(
                "Downloading the models for telling speakers apart: {downloaded_count} of {total}",
                downloaded_count=models.size_text(downloaded_count),
                total=models.size_text(total),
            )
        )
        self.diarization_status.show()

    def _diarization_finished(self, result: object) -> None:
        if result is None:
            self.diarization_status.hide()
            return
        log.warning("diarization models did not download: %s", result)
        style.restyle(self.diarization_status, "ошибка_текст")
        self.diarization_status.setText(
            tr(
                "The models for telling speakers apart did not download: {reason} They will download before "
                "transcription when there is internet.",
                reason=failures.text(result),
            )
        )

    def pick_model(self, download: bool = False) -> None:
        """The model window: search on Hugging Face, languages, mirror, own link; download: start the chosen one."""
        config = self._editor.settings
        if self._models_window is None:
            self._models_window = ModelsWindow(
                settings.models_folder(), config.models_mirror, config.language, self._page
            )
            self._models_window.chosen.connect(self._speech.set_model)
            self._models_window.chosen.connect(lambda _id: self.refresh())
            self._models_window.edited.connect(self.refresh)
            self._models_window.mirror_changed.connect(lambda address: self._editor.change(models_mirror=address))
        self._models_window.open_view(self._speech.chosen_model, config.speaker_diarization, download)
