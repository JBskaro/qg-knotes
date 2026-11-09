# SPDX-License-Identifier: GPL-3.0-only
"""The "Choose a model" window: downloaded models and models available to download (catalog, custom, Hugging Face).
The site list, sizes and downloads run in their own thread pools: they do not hold up other background tasks."""

import logging
import pathlib
import threading
from collections.abc import Callable

from PySide6.QtCore import Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from knotes import models, failures
from knotes.ui import style
from knotes.ui.background import run_in_background
from knotes.language import tr_key, tr

FILTER_FILES = [("ru", models.LANGUAGES["ru"]), ("en", models.LANGUAGES["en"]), ("", tr_key("Все языки"))]
CANCELLED = "отменена"  # result of a download that was stopped: not an error
BUTTONS = {models.PARTIAL: tr_key("Докачать"), models.BROKEN: tr_key("Скачать заново"), models.NONE: tr_key("Скачать")}
log = logging.getLogger(__name__)


def languages_text(model: models.Model) -> str:
    if "*" in model.languages or len(model.languages) > 2:
        return tr("Many languages")
    return ", ".join(tr(models.LANGUAGES[me]) if me in models.LANGUAGES else me.upper() for me in model.languages)


def description(model: models.Model, size: int | None, size_unknown: bool = False) -> str:
    """What the model can do and how much it weighs: "Russian · punctuation · 230 MB"; size None: not known yet,
    size_unknown: it could not be found out."""
    size_ = models.size_text(size) if size else tr("size unknown") if size_unknown else ""
    return " · ".join(ch for ch in (languages_text(model), tr("punctuation") if model.punctuation else "", size_) if ch)


def _text(text: str, name: str = "тихий_текст") -> QLabel:
    label = style.caption(text)  # Hugging Face titles as plain text
    label.setObjectName(name)
    label.setWordWrap(True)
    return label


def _button(row_label: str, action: Callable[[], object], name: str = "") -> QPushButton:
    button = QPushButton(row_label)
    button.setObjectName(name)
    button.setAutoDefault(False)  # Enter in the search does not press the button
    button.clicked.connect(action)
    return button


def _pool(thread_count: int) -> QThreadPool:
    pool = QThreadPool()
    pool.setMaxThreadCount(thread_count)
    return pool


class ModelsWindow(QDialog):
    """Signals: chosen(id) when a model is chosen (empty: the preset's model); edited() when a model is downloaded or
    deleted; mirror_changed(address). One window per settings window: open_view() shows it again, started downloads
    stay visible."""

    chosen = Signal(str)
    edited = Signal()
    mirror_changed = Signal(str)
    _site_arrived = Signal(int, object)  # request generation and the list or an error
    _size_arrived = Signal(str, object)
    _progress = Signal(str, int, int)  # model, downloaded, total, from the download thread
    _result = Signal(str, object)  # model and None (done), CANCELLED or an error
    _own_arrived = Signal(object)

    def __init__(self, folder: pathlib.Path, mirror: str, language: str, parent_: QWidget | None = None):
        super().__init__(parent_)
        self.setWindowTitle(tr("Choose a model - {name}", name=style.NAME))
        self.resize(660, 640)
        self._folder, self._mirror, self._language = folder, mirror, language if language in ("ru", "en") else ""
        self._selected, self._diarization = "", False
        self._site: list[models.Model] | None = None  # None: the list has not arrived yet
        self._site_error, self._generation = "", 0
        self._full_models: dict[str, models.Model] = {}  # site models with file sizes
        self._no_size: set[str] = set()  # the size could not be found out: not asked again in this window
        self._waiting: set[str] = set()
        self._cancels: dict[str, threading.Event] = {}
        self._downloaded_bytes: dict[str, tuple[int, int]] = {}
        self._progress_bars: dict[str, tuple[QProgressBar, QLabel]] = {}
        self._descriptions: dict[str, QLabel] = {}
        self._info_pool, self._download_pool = _pool(2), _pool(2)
        for chime, receiver in (
            (self._site_arrived, self._show_site),
            (self._size_arrived, self._show_size),
            (self._progress, self._show_progress),
            (self._result, self._download_finished),
            (self._own_arrived, self._show_own),
        ):
            chime.connect(receiver)
        if (program := QApplication.instance()) is not None:
            program.aboutToQuit.connect(self._stop_all)
        self._build()

    def _build(self) -> None:
        self._search = QLineEdit()
        self._search.setPlaceholderText(tr("Find a model"))
        self._search.setClearButtonEnabled(True)
        self._typing = QTimer(self)  # list is rebuilt once typing pauses, not on every letter
        self._typing.setSingleShot(True)
        self._typing.setInterval(200)
        self._typing.timeout.connect(self._fill_downloadable)
        self._search.textChanged.connect(lambda _text: self._typing.start())
        languages = style.Segments([(z, tr(p)) for z, p in FILTER_FILES], self._language)
        languages.selected.connect(self._change_language)
        top = QHBoxLayout()
        top.addWidget(self._search, 1)
        top.addWidget(languages)
        self._here, self._download = QVBoxLayout(), QVBoxLayout()
        self._place, self._state = _text("", "группа"), _text("")
        self._retry = _button(tr("Retry"), self._load_site, "назад")
        status = QHBoxLayout()
        status.addWidget(self._state, 1)
        status.addWidget(self._retry, 0, Qt.AlignmentFlag.AlignTop)
        contents_ = QWidget()
        column = QVBoxLayout(contents_)
        column.addWidget(self._place)
        column.addLayout(self._here)
        column.addWidget(_text(tr("Available to download"), "группа"))
        for part in (status, self._download):
            column.addLayout(part)
        column.addStretch()
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(contents_)
        bottom = QHBoxLayout()
        for button in (
            _button(tr("Set a mirror"), self._set_mirror, "назад"),
            _button(tr("Paste a link"), self._paste_link, "назад"),
        ):
            bottom.addWidget(button)
        bottom.addStretch()
        bottom.addWidget(_button(tr("Done"), self.accept))
        window = QVBoxLayout(self)
        window.addLayout(top)
        window.addWidget(style.layer(scroll_area, padding=2), 1)
        window.addLayout(bottom)

    def open_view(self, selected_one: str, diarization: bool, download: bool = False) -> None:
        """Shows the window with selected_one chosen; diarization: whether to download the speaker diarization models
        with the model; download: start downloading the chosen one at once. A site list that did not load last time is
        requested again."""
        self._selected, self._diarization = selected_one, diarization
        self.setStyleSheet(style.window_style())
        if (self._site is None or self._site_error) and "" not in self._waiting:
            self._load_site()
        self.refresh()
        if download:
            self._download_selected()
        self.exec()

    def _download_selected(self) -> None:
        try:
            model = models.find(self._selected, self._folder)
        except models.ModelError:
            return
        if not self._is_downloaded(model):
            self._start(model)

    def _load_site(self) -> None:
        """Requests the list; an answer to an earlier request (before the mirror changed) is dropped by generation."""
        self._generation += 1
        self._site, self._site_error = None, ""
        self._waiting.add("")  # an empty id stands for the list itself
        folder, mirror, generation = self._folder, self._mirror, self._generation
        run_in_background(
            self._info_pool,
            lambda: models.from_site(folder, mirror),
            lambda result: self._site_arrived.emit(generation, result),
        )
        self._fill_downloadable()

    def _show_site(self, generation: int, result: object) -> None:
        if generation != self._generation:
            return
        self._waiting.discard("")
        if isinstance(result, Exception):
            log.warning("model list from the site: %s", result)
            self._site, self._site_error = [], failures.text(result)
        else:
            self._site = result
        self._fill_downloadable()

    def _change_language(self, language: object) -> None:
        self._language = str(language)
        self._fill_downloadable()

    # --- sections -------------------------------------------------------------------------------------------

    def refresh(self) -> None:
        self._fill_local()
        self._fill_downloadable()

    def _is_downloaded(self, model: models.Model) -> bool:
        return models.status(self._folder, model) == models.DOWNLOADED

    def _downloaded(self) -> list[models.Model]:
        return [m for m in models.all_(self._folder) if not m.service_one and self._is_downloaded(m)]

    def _fill_local(self) -> None:
        style.empty(self._here)
        here = self._downloaded()
        self._place.setText(
            tr(
                "On this computer · {taken} used, {free} free",
                taken=models.size_text(models.size_on_disk(self._folder)),
                free=models.size_text(models.free(self._folder)),
            )
        )
        self._radio_buttons = QButtonGroup(self)
        for model in here:
            self._here.addWidget(self._local_row(model))
        if not here:
            self._here.addWidget(_text(tr("No models yet: download one below.")))

    def _local_row(self, model: models.Model) -> QWidget:
        radio_button = QRadioButton(tr(model.title))
        radio_button.setChecked(model.id == self._selected)
        radio_button.toggled.connect(lambda on: on and self._pick(model.id))
        self._radio_buttons.addButton(radio_button)
        return self._line(
            model,
            radio_button,
            [(tr("Delete"), lambda: self._delete(model))],
            models.size_on_disk(self._folder, model),
        )

    def _matches(self, model: models.Model) -> bool:
        search = self._search.text().strip().lower()
        return models.understands(model, self._language) and (
            not search or search in tr(model.title).lower() or search in models.repository(model).lower()
        )

    def _downloadable(self) -> tuple[list[models.Model], set[str]]:
        """Models not downloaded yet, in order: catalog (recommended first), custom, from the site; a custom one
        replaces the same one from the site. The second item is the ids of custom models."""
        custom_items = models.custom_items(self._folder)
        custom_count = {m.id for m in custom_items}
        catalog = sorted((m for m in models.catalog() if not m.service_one), key=lambda m: not m.recommended)
        site = [m for m in self._site or [] if m.id not in custom_count]
        return [
            m for m in [*catalog, *custom_items, *site] if not self._is_downloaded(m) and self._matches(m)
        ], custom_count

    def _fill_downloadable(self) -> None:
        style.empty(self._download)
        self._progress_bars.clear()
        self._descriptions.clear()
        visible_all, custom_count = self._downloadable()
        for model in visible_all:
            self._download.addWidget(self._download_row(model, model.id in custom_count))
        error = self._site_error
        self._state.setText(
            tr("Looking for models on Hugging Face…")
            if self._site is None
            else error or ("" if visible_all else tr("Nothing found. Change the language or the search."))
        )
        self._state.setObjectName("ошибка_текст" if error else "тихий_текст")
        self._state.setStyleSheet("")  # the name changed: restyle by the new name
        self._state.setVisible(bool(self._state.text()))
        self._retry.setVisible(bool(error))

    def _download_buttons(self, model: models.Model, custom: bool) -> list[tuple[str, Callable[[], None]]]:
        if model.id in self._cancels:
            return [(tr("Cancel"), self._cancels[model.id].set)]
        status = models.status(self._folder, model)
        buttons = [(tr(BUTTONS[status]), lambda: self._start(model))]
        if status != models.NONE:  # partly downloaded and broken files take up space
            buttons.append((tr("Delete"), lambda: self._delete(model)))
        if custom:
            buttons.append((tr("Remove"), lambda: self._remove(model)))
        return buttons

    def _download_row(self, model: models.Model, custom: bool) -> QWidget:
        title = QWidget()
        row = QHBoxLayout(title)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(style.caption(tr(model.title)))
        if model.recommended:
            chip = QLabel(tr("Recommended"))
            chip.setStyleSheet(style.chip("готова"))
            row.addWidget(chip)
        row.addStretch()
        return self._line(model, title, self._download_buttons(model, custom), self._size_of(model))

    def _line(self, model: models.Model, heading: QWidget, buttons: list, size: int | None) -> QWidget:
        """A model row: title, abilities and size, the catalog hint, download progress; buttons on the right."""
        left_part = QVBoxLayout()
        left_part.setSpacing(2)
        details_text = _text(description(model, size, model.id in self._no_size))
        self._descriptions[model.id] = details_text
        for widget in (heading, details_text, *([_text(tr(model.hint))] if model.hint else [])):
            left_part.addWidget(widget)
        bar, progress = QProgressBar(), _text("")
        bar.setObjectName("ход")
        bar.setTextVisible(False)
        self._progress_bars[model.id] = (bar, progress)
        for widget in (bar, progress):
            widget.setVisible(model.id in self._cancels)
            left_part.addWidget(widget)
        if model.id in self._downloaded_bytes:
            self._show_progress(model.id, *self._downloaded_bytes[model.id])
        row = QHBoxLayout()
        row.addLayout(left_part, 1)
        for row_label, action in buttons:
            row.addWidget(_button(row_label, action), 0, Qt.AlignmentFlag.AlignTop)
        line = QWidget()
        line.setLayout(row)
        info = [tr("License: {license}", license=model.license) if model.license else "", model.url]
        line.setToolTip(style.plain_tooltip("\n".join(ch for ch in info if ch)))
        return line

    def _size_of(self, model: models.Model) -> int | None:
        """Model size; a site model does not have it yet: its details are requested in the background, the result is
        None."""
        if (size := models.size(model)) is not None:
            return size
        if model.id in self._full_models:
            return models.size(self._full_models[model.id])
        if model.id not in self._waiting and model.id not in self._no_size:
            self._waiting.add(model.id)
            url, mirror, id_ = model.url, self._mirror, model.id
            run_in_background(
                self._info_pool, lambda: models.read(url, mirror), lambda result: self._size_arrived.emit(id_, result)
            )
        return None

    def _show_size(self, id_: str, result: object) -> None:
        self._waiting.discard(id_)
        if isinstance(result, models.Model):
            self._full_models[id_] = result
        else:
            log.warning("size of model %s: %s", id_, result)
            self._no_size.add(id_)
        if (label := self._descriptions.get(id_)) is not None:
            full_one = self._full_models.get(id_)
            label.setText(
                description(full_one, models.size(full_one))
                if full_one
                else label.text() + (" · " if label.text() else "") + tr("size unknown")
            )

    # --- choice, download, deletion --------------------------------------------------------------------------

    def _pick(self, id_: str) -> None:
        if id_ != self._selected:
            self._selected = id_
            self.chosen.emit(id_)

    def _pick_downloaded(self) -> None:
        """The chosen model was deleted: the first downloaded one will recognize, and if there is none, the preset's
        model."""
        if not any(m.id == self._selected for m in self._downloaded()):
            self._pick(next((m.id for m in self._downloaded()), ""))

    def _start(self, model: models.Model) -> None:
        """Downloads the model and the helper models it needs to work; a site model is first saved among the custom
        ones."""
        if model.id in self._cancels:
            return
        cancel = self._cancels[model.id] = threading.Event()
        folder, mirror, diarization, full_one = (
            self._folder,
            self._mirror,
            self._diarization,
            self._full_models.get(model.id),
        )

        def work() -> str | None:
            needed_one = model
            if model.custom and model.id not in {m.id for m in models.custom_items(folder)}:
                needed_one = full_one or models.read(model.url, mirror)
                if cancel.is_set():
                    return CANCELLED
                models.remember(folder, needed_one)
            try:
                models.download_all(
                    folder,
                    models.needed_models(needed_one, folder, diarization),
                    lambda downloaded_count, total: self._progress.emit(model.id, downloaded_count, total),
                    cancel,
                    mirror,
                )
            except models.DownloadCancelled:
                return CANCELLED
            return None

        run_in_background(self._download_pool, work, lambda result: self._result.emit(model.id, result))
        self._fill_downloadable()

    def _show_progress(self, id_: str, downloaded_count: int, total: int) -> None:
        self._downloaded_bytes[id_] = (downloaded_count, total)
        if id_ not in self._progress_bars:
            return
        bar, progress = self._progress_bars[id_]
        bar.setRange(0, 1000)
        bar.setValue(round(1000 * downloaded_count / total) if total else 0)
        progress.setText(
            tr(
                "Downloaded {downloaded_count} of {total}",
                downloaded_count=models.size_text(downloaded_count),
                total=models.size_text(total),
            )
        )
        bar.show()
        progress.show()

    def _download_finished(self, id_: str, result: object) -> None:
        self._cancels.pop(id_, None)
        self._downloaded_bytes.pop(id_, None)
        if isinstance(result, Exception):
            log.warning("model %s did not download: %s", id_, result)
            QMessageBox.warning(self, tr("The model did not download"), failures.text(result))
        if result is None and not any(m.id == self._selected for m in self._downloaded()):
            self._pick(id_)  # the chosen one is not downloaded: the one just downloaded will recognize
        self.refresh()
        self.edited.emit()

    def _delete(self, model: models.Model) -> None:
        size = models.size_text(models.size_on_disk(self._folder, model))
        if (
            QMessageBox.question(
                self,
                tr("Delete model"),
                tr('Delete the model "{title}"? {size} will be freed.', title=tr(model.title), size=size),
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._change(model, models.delete, tr("Delete model"))

    def _remove(self, model: models.Model) -> None:
        self._change(model, lambda folder, m: models.remove_custom(folder, m.id), tr("Remove the model"))

    def _change(self, model: models.Model, action: Callable, heading: str) -> None:
        """Deletes or removes a model; a failure (files are busy) goes to the log and is shown as a clear text. The
        first downloaded model replaces the chosen one."""
        try:
            action(self._folder, model)
        except models.ModelError as error:
            log.warning("%s %s: %s", heading, model.id, error)
            QMessageBox.warning(self, heading, str(error))
        self._pick_downloaded()
        self.refresh()
        self.edited.emit()

    # --- mirror and custom model ------------------------------------------------------------------------------

    def _set_mirror(self) -> None:
        address, yes = QInputDialog.getText(
            self,
            tr("Hugging Face mirror"),
            tr(
                "Address to download models from if huggingface.co does not open, for example https://hf-mirror.com. "
                "Empty - huggingface.co."
            ),
            text=self._mirror,
        )
        if not yes:
            return
        try:
            address = models.mirror_address(address)
        except models.ModelError as error:
            QMessageBox.warning(self, tr("Hugging Face mirror"), str(error))
            return
        self._mirror = address
        self.mirror_changed.emit(address)
        self._no_size.clear()
        self._load_site()

    def _paste_link(self) -> None:
        url, yes = QInputDialog.getText(
            self, tr("Your own model"), tr("Link to a Hugging Face model or owner/model name:")
        )
        if not yes or not url.strip():
            return
        self._state.setText(tr("Reading the model on Hugging Face…"))
        self._state.show()
        mirror = self._mirror
        run_in_background(self._info_pool, lambda: models.read(url, mirror), self._own_arrived.emit)

    def _show_own(self, result: object) -> None:
        if isinstance(result, Exception):
            log.warning("custom model by link: %s", result)
            self._fill_downloadable()
            QMessageBox.warning(self, tr("Your own model"), failures.text(result))
            return
        self._search.clear()
        self._full_models[result.id] = result
        self._start(result)  # the link was pasted to download it: the download starts at once

    def _stop_all(self) -> None:
        """On program exit: downloads stop (.part stays to resume), pending info requests are dropped."""
        for cancel in self._cancels.values():
            cancel.set()
        self._info_pool.clear()

    def done(self, result: int) -> None:
        """In the middle of a download asks whether to stop it; .part stays to resume."""
        if (
            self._cancels
            and QMessageBox.question(
                self,
                tr("The model is still downloading"),
                tr('Stop the download? It will resume from the same place when you press "Download" again.'),
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        for cancel in self._cancels.values():
            cancel.set()
        super().done(result)
