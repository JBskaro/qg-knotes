# SPDX-License-Identifier: GPL-3.0-only
"""First-run setup in three steps: check the microphone and the participants' sound, choose speech recognition,
make a 10-second test recording and see its text.

The model for recognition on this computer downloads in an object that outlives the window: closing the setup does
not stop it, and its end is told in a message at the top of the screen. The settings download their models the same
way."""

import dataclasses
import html
import logging
import pathlib
import shutil
import tempfile
import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal, SignalInstance
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from knotes import recording, models, failures, player, providers, transcript
from knotes import settings
from knotes import recognition
from knotes.ui import form, style
from knotes.ui.background import run_in_background
from knotes.ui.recording_bar import loudness_share
from knotes.ui.toast import Toast
from knotes.language import tr_key, tr

log = logging.getLogger(__name__)
TEST_SECONDS = 10
HEARD = 0.15  # level share that counts as heard, above room noise
HEARD_FOR_S = 1.5  # "Heard" stays this long after the last loud moment
TICK_MS = 100
TITLES = (tr_key("Проверим звук"), tr_key("Чем распознавать речь"), tr_key("Пробная запись"))
# the recording's own text for a missing output device: it is about the participants' sound
NO_OUTPUT = (
    "No headphones or speakers found, so other participants can't be recorded. Connect them and start recording again"
)


def transcribe(folder: pathlib.Path, params: recognition.Params) -> list[dict]:
    """Recognizes the test recording; its phrases."""
    recognition.process(folder, params)
    return (transcript.phrases(folder) or {}).get("фразы", [])


def _emit(signal: SignalInstance, *values: object) -> None:
    """From the recording thread to the window: the window may be gone by then."""
    try:
        signal.emit(*values)
    except RuntimeError:
        log.info("a sound check event came after the setup closed")


class _Probe:
    """A recording into its own temporary folder: its levels show whether the devices are heard."""

    def __init__(self, devices: tuple[str, str], events: recording.Events):
        self.folder = pathlib.Path(tempfile.mkdtemp(prefix="knotes_проверка_"))
        self.recording = recording.Recording(self.folder, events, devices)
        self.recording.start_now()

    def remove(self) -> None:
        """Stops without assembling the sound: with the chunk list gone the recording thread has nothing to join. The
        folder goes once the thread ends, in the background: the window does not wait for it."""
        (self.folder / recording.MANIFEST).unlink(missing_ok=True)
        self.recording.stop()
        threading.Thread(target=self._clean, name="проверка звука", daemon=True).start()

    def _clean(self) -> None:
        self.recording.wait(30)
        shutil.rmtree(self.folder, ignore_errors=True)


@dataclasses.dataclass(frozen=True)
class LocalModel:
    """The model for recognition on this computer and what is left to download for it: the model itself and, with
    telling speakers apart on, the service models."""

    model: models.Model | None  # None: the chosen model is not known
    missing: tuple[models.Model, ...] = ()

    @property
    def downloaded(self) -> bool:
        return self.model is not None and self.model not in self.missing

    @property
    def size(self) -> int:
        """Bytes to download; 0 if the sizes are not known."""
        return sum(models.size(m) or 0 for m in self.missing)


def local_model(model_id: str, diarization: bool) -> LocalModel:
    folder = settings.models_folder()
    try:
        model = models.find(model_id, folder)
    except models.ModelError:
        return LocalModel(None)
    return LocalModel(model, tuple(models.needed_models(model, folder, diarization)))


def _tell(heading: str, text: str) -> None:
    """A message at the top of the screen, as the program gives them: the window that started the work is gone."""
    message = Toast(heading, text, [])
    _messages.append(message)
    message.choice.connect(lambda _answer: _messages.remove(message))
    message.top_center()
    message.appear()


_messages: list[Toast] = []


class ModelDownload(QObject):
    """A download of models that outlives the window that started it. Windows follow it with follow(); when it ends
    and no window follows it any more, the end is told in a message at the top of the screen."""

    progress = Signal(int, int)  # bytes done, total
    finished = Signal(object)  # None or the error

    def __init__(self, title: str):
        super().__init__(QApplication.instance())  # lives as long as the program
        self.title, self.running = title, False
        self._followers: set[int] = set()

    def start(self, folder: pathlib.Path, list_: list[models.Model], mirror: str) -> None:
        self.running = True
        run_in_background(
            QThreadPool.globalInstance(),
            lambda: models.download_all(folder, list_, self.progress.emit, None, mirror),
            self._done,
        )

    def follow(self, window: QObject, progress: Callable[[int, int], None], finished: Callable[[object], None]) -> None:
        self.progress.connect(progress)
        self.finished.connect(finished)
        number = id(window)
        self._followers.add(number)
        window.destroyed.connect(lambda _window=None: self._followers.discard(number))

    def unfollow(self, window: QObject, progress: Callable, finished: Callable) -> None:
        if id(window) in self._followers:
            self._followers.discard(id(window))
            self.progress.disconnect(progress)
            self.finished.disconnect(finished)

    def _done(self, result: object) -> None:
        self.running = False
        failed = isinstance(result, Exception) and not isinstance(result, models.DownloadCancelled)
        if failed:
            log.warning("models did not download: %s", result)
        self.finished.emit(result)
        if self._followers:
            return
        if failed:
            _tell(
                tr("Not downloaded: {title}", title=self.title),
                tr("{reason} Download again in Settings, Speech recognition.", reason=failures.text(result)),
            )
        else:
            _tell(tr("Downloaded: {title}", title=self.title), tr("Speech is recognized on this computer now."))


_downloads: dict[frozenset[str], ModelDownload] = {}


def download(list_: list[models.Model], title: str) -> ModelDownload:
    """The download of these models: the one running, so the same models are not downloaded twice at once, or a new
    one; the caller follows it and starts it unless it runs."""
    key = frozenset(m.id for m in list_)
    job = _downloads.get(key)
    if job is None or not job.running:
        job = _downloads[key] = ModelDownload(title)
    return job


def _text(line: str = "", name: str = "") -> QLabel:
    label = QLabel(line)
    label.setWordWrap(True)
    if name:
        label.setObjectName(name)
    return label


def _level() -> QProgressBar:
    bar = QProgressBar()
    bar.setObjectName("громкость")
    bar.setTextVisible(False)
    bar.setRange(0, 100)
    return bar


def _set_state(label: QLabel, line: str, name: str) -> None:
    label.setText(line)
    style.restyle(label, name)


def _qss() -> str:
    dark = style.dark()
    track, border = (
        ("rgba(255,255,255,0.10)", "rgba(255,255,255,0.16)") if dark else ("rgba(0,0,0,0.08)", "rgba(0,0,0,0.14)")
    )
    return f"""
QProgressBar#громкость {{ border: none; border-radius: 3px; background: {track}; max-height: 6px; }}
QProgressBar#громкость::chunk {{ background: #97C459; border-radius: 3px; }}
QLabel#шаг {{ font-size: 20px; font-weight: 600; }}
QFrame#выбор {{ border: 1px solid {border}; border-radius: 8px; }}
"""


class SetupWizard(QDialog):
    """changed(settings) - after every choice; closed(how): "done" or "skipped" (also the window's cross).
    Background work runs in the global pool: closing the setup does not wait for it, a late result is dropped."""

    changed = Signal(object)
    closed = Signal(str)
    _recorded = Signal(object, str)  # folder and error of the test recording, from its thread
    _probe_note = Signal(str)  # what the sound check recording says: a device missing, busy or replaced

    def __init__(self, settings: settings.Settings, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = dataclasses.replace(settings)
        self.setObjectName("основа")
        self.setWindowTitle(tr("Setup - {name}", name=style.NAME))
        self.resize(620, 500)
        self._probe: _Probe | None = None  # step 1: levels only
        self._test: _Probe | None = None  # step 3: the test recording
        self._started = 0.0
        self._heard = {"микрофон": 0.0, "собеседники": 0.0}  # when each track was last heard
        self._downloading, self._download_error, self._download_state = False, "", ""
        self._job: ModelDownload | None = None  # the model download this window follows
        self._waiting: pathlib.Path | None = None  # recorded, waits for the model
        self._done = False
        self._recorded.connect(self._on_recorded)
        self._probe_note.connect(self._on_probe_note)
        self._timer = QTimer(self, interval=TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._build()
        self.setStyleSheet(style.window_style() + _qss())
        self._show(0)
        self._timer.start()

    # --- layout ---------------------------------------------------------------------------------------------

    def _build(self) -> None:
        self._step, self._title = _text(name="тихий_текст"), _text(name="шаг")
        self._pages = QStackedWidget()
        for page in (self._sound_page(), self._recognition_page(), self._test_page()):
            self._pages.addWidget(page)
        self._skip, self._back, self._next = QPushButton(tr("Skip")), QPushButton(tr("Back")), QPushButton()
        self._skip.setObjectName("назад")
        self._next.setObjectName("главная")
        for button in (self._skip, self._back, self._next):
            button.setAutoDefault(False)
        self._skip.clicked.connect(lambda: self._finish("skipped"))
        self._back.clicked.connect(lambda: self._show(self._pages.currentIndex() - 1))
        self._next.clicked.connect(self._forward)
        foot = QHBoxLayout()
        foot.addWidget(self._skip)
        foot.addStretch()
        foot.addWidget(self._back)
        foot.addWidget(self._next)
        column = QVBoxLayout(self)
        column.setContentsMargins(28, 22, 28, 20)
        column.addWidget(self._step)
        column.addWidget(self._title)
        column.addSpacing(6)
        column.addWidget(self._pages, 1)
        column.addLayout(foot)

    def _devices(self, field: str, names: list[str]) -> QComboBox:
        box = form.devices(names, getattr(self.settings, field))
        box.currentIndexChanged.connect(lambda: self._choose_device(field, box.currentData()))
        return box

    def _sound_page(self) -> QWidget:
        try:
            mics, outputs = recording.device_list()
        except OSError:  # the Windows audio service is down: only "Windows default"
            mics, outputs = [], []
        self._mic, self._out = self._devices("microphone", mics), self._devices("output", outputs)
        self._check = QPushButton(tr("Check"))
        self._check.clicked.connect(self._chime)
        self._mic_level, self._out_level = _level(), _level()
        self._mic_state, self._out_state = _text(name="тихий_текст"), _text(name="тихий_текст")
        self._no_mic = not mics
        if self._no_mic:
            _set_state(
                self._mic_state,
                tr("No microphone found: connect a microphone or a headset and open this check again from the help."),
                "ошибка_текст",
            )
        out_row = QHBoxLayout()
        out_row.addWidget(self._out, 1)
        out_row.addWidget(self._check)
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(_text(tr('Say something and click "Check": both bars should move.'), "второй_текст"))
        column.addSpacing(10)
        for title, row, bar, state in (
            (tr("Microphone"), self._mic, self._mic_level, self._mic_state),
            (tr("Other participants' audio"), out_row, self._out_level, self._out_state),
        ):
            column.addWidget(QLabel(title))
            if isinstance(row, QHBoxLayout):
                column.addLayout(row)
            else:
                column.addWidget(row)
            column.addWidget(bar)
            column.addWidget(state)
            column.addSpacing(10)
        column.addStretch()
        return page

    def _card(self, choice: QRadioButton, note: QLabel, *below: QWidget) -> QFrame:
        card = QFrame()
        card.setObjectName("выбор")
        column = QVBoxLayout(card)
        column.setContentsMargins(14, 12, 14, 12)
        choice.setStyleSheet("font-weight: 600;")
        column.addWidget(choice)
        for part in (note, *below):
            part.setContentsMargins(26, 0, 0, 0)
            column.addWidget(part)
        return card

    def _recognition_page(self) -> QWidget:
        self._local, self._service = QRadioButton(tr("On this computer")), QRadioButton(tr("Through a service"))
        self._choice = QButtonGroup(self)  # the buttons sit in different cards: one group keeps one chosen
        self._choice.addButton(self._local)
        self._choice.addButton(self._service)
        self._services = [p for p in providers.list_("распознавание") if not p.local_engine and p.key_needed]
        self._preset = QComboBox()
        for service in self._services:
            self._preset.addItem(tr(service.title), service.id)
        self._key = QLineEdit()
        self._key.setPlaceholderText(tr("API key"))
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._verify = QPushButton(tr("Check"))
        self._verify.clicked.connect(self._check_key)
        self._key_state = _text(name="тихий_текст")
        fields = QWidget()
        row = QHBoxLayout(fields)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self._preset)
        row.addWidget(self._key, 1)
        row.addWidget(self._verify)
        self._fields = [fields, self._key_state]
        self._local_note = _text(name="второй_текст")
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(12)
        column.addWidget(_text(tr("You can change it later in the settings."), "второй_текст"))
        column.addWidget(self._card(self._local, self._local_note))
        column.addWidget(
            self._card(
                self._service,
                _text(tr("Faster on a slow computer. Paid, the service's website issues the key."), "второй_текст"),
                *self._fields,
            )
        )
        column.addStretch()
        local = providers.connection("распознавание", self.settings.provider, self.settings.address).local_engine
        self._preset.setCurrentIndex(max(0, self._preset.findData(self.settings.provider)))
        (self._local if local else self._service).setChecked(True)
        self._service.toggled.connect(self._show_fields)
        self._preset.currentIndexChanged.connect(self._fill_key)
        self._show_fields()
        self._fill_key()
        return page

    def _test_page(self) -> QWidget:
        self._record = QPushButton()
        self._record.clicked.connect(self._record_clicked)
        self._test_level, self._countdown = _level(), _text(name="второй_текст")
        self._state, self._result = _text(name="тихий_текст"), _text()
        self._result_card = QFrame()
        self._result_card.setObjectName("слой")
        inside = QVBoxLayout(self._result_card)
        inside.setContentsMargins(14, 12, 14, 12)
        inside.addWidget(self._result)
        row = QHBoxLayout()
        row.addWidget(self._record)
        row.addWidget(self._test_level, 1)
        row.addWidget(self._countdown)
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)
        column.addWidget(
            _text(tr('Click "Record" and say a couple of phrases. The text appears after 10 seconds.'), "второй_текст")
        )
        column.addLayout(row)
        column.addWidget(self._result_card)
        column.addWidget(self._state)
        column.addStretch()
        self._record_button(tr("Record 10 seconds"))
        return page

    # --- steps ----------------------------------------------------------------------------------------------

    def _show(self, index: int) -> None:
        if self._pages.currentIndex() == 0 and index != 0:
            self._stop_probe()
        if self._pages.currentIndex() == 2 and index != 2:
            self._drop_test()
        self._pages.setCurrentIndex(index)
        self._step.setText(tr("Step {number} of {total}", number=index + 1, total=len(TITLES)))
        self._title.setText(tr(TITLES[index]))
        last = index == len(TITLES) - 1
        self._skip.setVisible(not last)
        self._back.setVisible(index > 0)
        self._next.setText(tr("Done") if last else tr("Next"))
        if index == 0:
            self._probe = self._sound_check()
        if index == 2 and self._download_error:  # seen at once, not only after the test recording
            _set_state(self._state, self._download_error, "ошибка_текст")
        if index == 1:
            self._local_note.setText(self._local_text())

    def _forward(self) -> None:
        index = self._pages.currentIndex()
        if index == 1:
            self._apply_recognition()
        if index == len(TITLES) - 1:
            self._finish("done")
        else:
            self._show(index + 1)

    def _finish(self, how: str) -> None:
        if self._done:
            return
        self._done = True
        self._timer.stop()
        self._stop_probe()
        self._drop_test()
        self._unfollow()
        self.closed.emit(how)
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._finish("skipped")
        super().closeEvent(event)

    def _changed(self, **fields: object) -> None:
        self.settings = dataclasses.replace(self.settings, **fields)
        self.changed.emit(self.settings)

    # --- step 1: sound --------------------------------------------------------------------------------------

    def _sound_check(self) -> _Probe:
        """Levels only; what the recording says about the devices shows under their bars."""
        events = recording.Events(
            change_item=lambda text: _emit(self._probe_note, text),
            ended=lambda _folder, error: error and _emit(self._probe_note, error),
        )
        return _Probe((self.settings.microphone, self.settings.output), events)

    def _on_probe_note(self, text: str) -> None:
        """A missing output device and the chosen output replaced are about the participants' sound, the rest
        about the microphone."""
        output = self.settings.output
        participants = (output and output in text) or text == tr(NO_OUTPUT)
        _set_state(self._out_state if participants else self._mic_state, text, "ошибка_текст")

    def _stop_probe(self) -> None:
        if self._probe is not None:
            self._probe.remove()
            self._probe = None

    def _choose_device(self, field: str, name: str) -> None:
        self._changed(**{field: name})
        self._heard = {"микрофон": 0.0, "собеседники": 0.0}
        for label in (self._mic_state, self._out_state):  # a note on the old device no longer holds
            if not (label is self._mic_state and self._no_mic):
                _set_state(label, "", "тихий_текст")
        self._stop_probe()
        self._probe = self._sound_check()

    def _chime(self) -> None:
        run_in_background(QThreadPool.globalInstance(), lambda: player.chime(self.settings.output), self._chime_done)

    def _chime_done(self, result: object) -> None:
        if isinstance(result, Exception):
            _set_state(
                self._out_state, tr("Could not play the sound: {reason}", reason=failures.text(result)), "ошибка_текст"
            )

    def _tick(self) -> None:
        probe = self._probe or self._test
        if probe is None:
            return
        now, levels = time.monotonic(), probe.recording.levels()
        for track, bar in (("микрофон", self._mic_level), ("собеседники", self._out_level)):
            share = loudness_share(levels.get(track, 0))
            bar.setValue(int(share * 100))
            if track == "микрофон":
                self._test_level.setValue(int(share * 100))
            if share >= HEARD:
                self._heard[track] = now
        self._show_heard(now)
        if self._test is not None and self._started:
            self._count_down(now)

    def _show_heard(self, now: float) -> None:
        for track, label, silent in (
            ("микрофон", self._mic_state, tr("Not heard: say something or choose another microphone")),
            ("собеседники", self._out_state, tr('Not heard: click "Check" to play a sound')),
        ):
            no_mic = track == "микрофон" and self._no_mic
            if no_mic or (label.objectName() == "ошибка_текст" and not self._heard[track]):
                continue
            heard = bool(self._heard[track]) and now - self._heard[track] <= HEARD_FOR_S
            _set_state(label, "✓ " + tr("Heard") if heard else silent, "успех" if heard else "тихий_текст")

    # --- step 2: recognition --------------------------------------------------------------------------------

    def _show_fields(self) -> None:
        for part in self._fields:
            part.setVisible(self._service.isChecked())

    def _chosen_service(self) -> providers.Provider:
        return self._services[max(0, self._preset.currentIndex())]

    def _fill_key(self) -> None:
        service = self._chosen_service()
        self._key.setText(settings.address_key(service.address, service.key, service.variable))
        _set_state(
            self._key_state,
            tr("Get the key at {site}.", site=service.key_site) if service.key_site else "",
            "тихий_текст",
        )

    def _check_key(self) -> None:
        service, key = self._chosen_service(), self._key.text().strip()
        _set_state(self._key_state, tr("Checking…"), "тихий_текст")
        run_in_background(QThreadPool.globalInstance(), lambda: providers.check_key(service, key), self._key_checked)

    def _key_checked(self, result: object) -> None:
        if isinstance(result, Exception) or result:
            _set_state(
                self._key_state, failures.text(result) if isinstance(result, Exception) else str(result), "ошибка_текст"
            )
        else:
            _set_state(self._key_state, "✓ " + tr("Key accepted"), "успех")

    def _local_model(self) -> LocalModel:
        """The model for recognition on this computer and what to download for it."""
        params = recognition.params(dataclasses.replace(self.settings, provider=providers.LOCAL))
        return local_model(params.final_model, self.settings.speaker_diarization)

    def _local_text(self) -> str:
        state = self._local_model()
        if not state.missing:
            return tr("Free, the audio goes nowhere. The model is already downloaded.")
        size = state.size
        return (
            tr(
                "Free, the audio goes nowhere. The {size} model is downloaded once.",
                size=models.size_text(size).replace(" ", " "),
            )
            if size
            else tr("Free, the audio goes nowhere. The model is downloaded once.")
        )

    def _apply_recognition(self) -> None:
        """The model download counts only for recognition on this computer: a service does not wait for it."""
        self._unfollow()
        self._downloading, self._download_error, self._download_state, self._waiting = False, "", "", None
        if self._local.isChecked():
            self._changed(provider=providers.LOCAL)
            self._download()
            return
        service, key = self._chosen_service(), self._key.text().strip()
        if key:
            settings.set_address_key(service.address, key)
        self._changed(provider=service.id, address="", model="")

    def _download(self) -> None:
        """Starts the download, or follows the one already going: closing the setup does not stop it."""
        needed = list(self._local_model().missing)
        if not needed:
            return
        self._downloading = True
        self._job = download(needed, tr("the speech recognition model"))
        self._job.follow(self, self._on_progress, self._downloaded)
        if not self._job.running:
            self._job.start(settings.models_folder(), needed, self.settings.models_mirror)

    def _unfollow(self) -> None:
        if self._job is not None:
            self._job.unfollow(self, self._on_progress, self._downloaded)
            self._job = None

    def _on_progress(self, done: int, total: int) -> None:
        self._download_state = tr(
            "The model is downloading: {downloaded_count} of {total}.",
            downloaded_count=models.size_text(done),
            total=models.size_text(total),
        )
        if self._waiting is not None:
            _set_state(
                self._state, self._download_state + " " + tr("I'll recognize it once it downloads."), "тихий_текст"
            )

    def _downloaded(self, result: object) -> None:
        self._downloading = False
        if isinstance(result, Exception) and not isinstance(result, models.DownloadCancelled):
            self._download_error = tr("The model did not download: {reason}", reason=failures.text(result))
            if self._pages.currentIndex() == 2:
                _set_state(self._state, self._download_error, "ошибка_текст")
        if self._waiting is not None and not self._done:
            folder, self._waiting = self._waiting, None
            self._on_recorded(folder, "")

    # --- step 3: test recording -----------------------------------------------------------------------------

    def _record_button(self, caption: str, stop: bool = False) -> None:
        self._record.setText(("■  " if stop else "●  ") + caption)
        style.restyle(self._record, "стоп" if stop else "главная")
        widest = max(tr("Record 10 seconds"), tr("Record again"), tr("Stop"), key=len)
        self._record.setMinimumWidth(self._record.fontMetrics().horizontalAdvance("●  " + widest) + 48)

    def _record_clicked(self) -> None:
        if self._test is not None and self._started:
            self._stop_test()
            return
        self._drop_test()
        self._result_card.hide()
        _set_state(self._state, tr("Speak: recording."), "тихий_текст")
        self._test = _Probe(
            (self.settings.microphone, self.settings.output),
            recording.Events(ended=lambda folder, error: _emit(self._recorded, folder, error)),
        )
        self._started = time.monotonic()
        self._record_button(tr("Stop"), stop=True)

    def _count_down(self, now: float) -> None:
        left = TEST_SECONDS - (now - self._started)
        self._countdown.setText(tr("Recording: {n} s", n=max(0, int(left + 0.99))))
        if left <= 0:
            self._stop_test()

    def _stop_test(self) -> None:
        self._started = 0.0
        self._countdown.setText("")
        self._record.setEnabled(False)
        _set_state(self._state, tr("Recognizing…"), "тихий_текст")
        if self._test is not None:
            self._test.recording.stop()

    def _on_recorded(self, folder: pathlib.Path, error: str) -> None:
        if self._test is None or folder != self._test.folder:
            return  # an older test recording: it was dropped
        if error or self._download_error:
            self._show_result(error or self._download_error)
        elif self._downloading:
            self._waiting = folder
            _set_state(
                self._state,
                (self._download_state or tr("The model is downloading."))
                + " "
                + tr("I'll recognize it once it downloads."),
                "тихий_текст",
            )
        else:
            params = dataclasses.replace(
                recognition.params(self.settings), diarization=False
            )  # one speaker: faster without it
            run_in_background(QThreadPool.globalInstance(), lambda: transcribe(folder, params), self._show_result)

    def _show_result(self, result: object) -> None:
        """The text of the test recording, or why there is none: an error, or a clear text of it as is."""
        self._record.setEnabled(True)
        self._record_button(tr("Record again"))
        if isinstance(result, (Exception, str)):
            reason = failures.text(result) if isinstance(result, Exception) else result
            _set_state(self._state, tr("{reason} Go back a step and check your choice.", reason=reason), "ошибка_текст")
        elif not result:
            _set_state(
                self._state,
                tr("No text: the recording is silent. Check the microphone in the first step and record again."),
                "ошибка_текст",
            )
        else:
            self._result.setText(
                "<br>".join(f"<b>{html.escape(tr(f['кто']))}</b>&nbsp; {html.escape(f['текст'])}" for f in result)
            )
            self._result_card.show()
            _set_state(self._state, "✓ " + tr("Everything works. This is how your meetings will look."), "успех")
        self._drop_test()

    def _drop_test(self) -> None:
        self._started = 0.0
        if self._test is not None:
            self._test.remove()
            self._test = None
