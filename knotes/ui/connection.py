# SPDX-License-Identifier: GPL-3.0-only
"""Connection to an AI service in the settings, the same for speech recognition and for minutes: preset, address,
API key and model. The preset fills in the address and model; the address can be changed to any service with an
OpenAI-compatible API. The key is stored per address, so speech recognition and minutes on one server share it.

An answer of the service (the key check, the model list) belongs to the preset and address it was asked for: one
that comes after they changed is dropped.
"""

import logging
import urllib.parse
from collections.abc import Callable

from PySide6.QtCore import QObject, QThreadPool, Signal
from PySide6.QtWidgets import QComboBox, QFormLayout, QLabel, QLineEdit, QWidget

from knotes import failures, providers, language
from knotes import settings
from knotes.ui import form
from knotes.ui import style
from knotes.ui.background import run_in_background
from knotes.language import tr

# settings fields of each function: preset, custom address, custom model
FIELDS = {
    "распознавание": ("provider", "address", "model"),
    "протоколы": ("minutes_service", "minutes_address", "minutes_model_id"),
}
HINTS = {
    "распознавание": {
        "заготовка": language.tr_key(
            "Готовые настройки сервиса, который превращает звук в текст: подставляют адрес и "
            "модель. Запись уходит к нему только после вашего согласия."
        ),
        "модель": language.tr_key(
            "Выберите из списка или впишите вручную. По умолчанию ElevenLabs Scribe v2: хорошо "
            "понимает русский и различает говорящих."
        ),
    },
    "протоколы": {
        "заготовка": language.tr_key(
            "Готовые настройки языковой модели, которая составляет черновик протокола: "
            "подставляют адрес и модель. Расшифровка уходит к ней только по кнопке «Составить "
            "протокол»."
        ),
        "модель": language.tr_key(
            "Выберите из списка или впишите вручную. По умолчанию Claude Sonnet 5.5: точно "
            "выделяет решения и задачи, встреча около 40 минут стоит примерно 0,15-0,2 $."
        ),
    },
}
ADDRESS_HINT = language.tr_key(
    "Адрес сервиса до /v1, как в документации OpenAI. Окончание запроса программа добавит сама. "
    "Подойдет любой сервис с API как у OpenAI."
)
KEY = language.tr_key(
    "Ключ доступа к сервису. Хранится в диспетчере учетных данных Windows, не в файле. Ключ привязан "
    "к адресу: у распознавания и протоколов на одном сервере он общий."
)
SEPARATOR = "  ·  "  # between the model name and its id in the list
NOT_A_LIST = (KeyError, TypeError, AttributeError)  # what a model list of another form gives when read
log = logging.getLogger(__name__)


def _server(address: str) -> str:
    return urllib.parse.urlsplit(address.strip()).netloc.lower()


def checking(connection: providers.Provider, key: str) -> tuple[str, list[tuple[str, str]]]:
    """What is wrong (empty: all is fine) and the service models, if it returns their list. A refusal goes to the log
    with the server, not the key."""
    error = providers.check_key(connection, key)
    if error:
        log.warning("the key check at %s failed: %s", _server(connection.address), error)
    if error or not connection.models_endpoint:
        return error, []
    try:
        return "", providers.models(connection, key)
    except (providers.ProviderError, *NOT_A_LIST) as error:  # the key is accepted but no list came: still passed
        log.warning("no model list from %s: %r", _server(connection.address), error)
        return "", []


def _list_failure(error: Exception) -> str:
    """A model list in a form the app does not know (a model without "id") is said so, not as a bare "'id'"."""
    if isinstance(error, NOT_A_LIST):
        return tr("The server sent its model list in a form the app does not understand: type the model name by hand.")
    return failures.text(error)


class ConnectionBlock(QObject):
    """Form rows of one function. Signal edited(fields): the settings fields after an edit; the key is saved to the
    Windows credential store at once. other: (section name, its connection address): one server shares the key."""

    edited = Signal(dict)

    def __init__(
        self, function: str, config: settings.Settings, other: tuple[str, Callable[[], str]], parent_: QWidget
    ):
        super().__init__(parent_)
        self.function, self._other = function, other
        id_, custom_address, custom_model = (getattr(config, field) for field in FIELDS[function])
        self._id = providers.connection(function, id_, custom_address).id
        self._address, self._model = custom_address, custom_model
        # local speech recognition keeps its model in its own field: changing the preset does not erase it
        self._local_model = config.local_model if function == "распознавание" else ""
        self._form: QFormLayout | None = None  # where the rows went: service rows are hidden for the local preset
        self._network_rows: list[QWidget] = []
        self._generation = 0  # grows when the preset or address changes: older answers are dropped
        self._create(parent_)
        self._fill()

    @property
    def connection(self) -> providers.Provider:
        return providers.connection(self.function, self._id, self._address)

    def _preset(self) -> providers.Provider:
        return providers.find(self._id, self.function)

    def _create(self, parent_: QWidget) -> None:
        """Widgets are born inside the window: one shown before it has a parent flashes as a window of its own."""
        self.preset = QComboBox(parent_)
        self.preset.setMinimumWidth(300)
        for pr in providers.list_(self.function):
            title = tr(pr.title)
            self.preset.addItem(title if pr.checked else tr("{title} (not verified)", title=title), pr.id)
        self.preset.setCurrentIndex(self.preset.findData(self._id))
        self.preset.currentIndexChanged.connect(self._change_preset)
        self._saved = {field: form.SavedMark(parent_) for field in ("адрес", "модель")}
        self.address = QLineEdit(parent_)
        self.address.editingFinished.connect(self._set_address)
        self._plain_http_warning = QLabel(
            tr(
                "Without https the key and data would travel over the network unencrypted, so the app won't send them "
                "to this URL. Use https:// or a server on this computer or your local network."
            ),
            parent_,
        )
        self._plain_http_warning.setObjectName("ошибка_текст")
        self._plain_http_warning.setWordWrap(True)
        self.address.textChanged.connect(
            lambda text: self._plain_http_warning.setVisible(providers.insecure_address(text))
        )
        self.secret = form.SecretField(
            tr("Ask the service whether it accepts the key and get its list of models"), parent_
        )  # the line under the key also says why the model list did not come
        self.secret.field.editingFinished.connect(self.save_key)
        self._eye = style.icon_button(style.EYE, tr("Show key"), parent_=parent_)
        self._eye.setCheckable(True)
        self._eye.toggled.connect(
            lambda on: self.secret.field.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)
        )
        self.secret.check.clicked.connect(self._check)
        self.shared_label = QLabel(parent_)
        self.shared_label.setObjectName("тихий_текст")
        self.model = QComboBox(parent_)
        self.model.setEditable(True)
        self.model.setMinimumWidth(340)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model.activated.connect(lambda: self.set_model(self.model.currentData() or ""))
        self.model.lineEdit().editingFinished.connect(
            lambda: self.set_model(self.model.currentText().rsplit(SEPARATOR, 1)[-1].strip())
        )
        self._refresh = style.icon_button(style.REFRESH, tr("Refresh model list"), self._load_models, parent_)

    def into_form(self, form_layout: QFormLayout) -> None:
        """Rows into the section form: preset, address, API key (with the check result) and model."""
        hints = HINTS[self.function]
        self._form = form_layout
        model_row = form.row(self.model, self._refresh, self._saved["модель"], stretch=True)
        if self.function == "протоколы":
            manual = QLabel(tr("Choose a model from the list or paste its ID from the provider and press Enter."))
            manual.setObjectName("тихий_текст")
            manual.setWordWrap(True)
            model_row = form.column(model_row, manual)
        self._network_rows = [
            form.column(form.row(self.address, self._saved["адрес"], stretch=True), self._plain_http_warning),
            form.column(
                form.row(self.secret.field, self._eye, self.secret.check, self.secret.saved, stretch=True),
                self.secret.result,
                self.shared_label,
            ),
            model_row,
        ]
        form_layout.addRow(form.row_label(tr("Preset"), tr(hints["заготовка"])), form.row(self.preset))
        for row_label, hint, line in zip(
            (tr("URL"), tr("API key"), tr("Model")),
            (tr(ADDRESS_HINT), tr(KEY), tr(hints["модель"])),
            self._network_rows,
            strict=True,
        ):
            form_layout.addRow(form.row_label(row_label, hint), line)
        self._show_rows()

    @property
    def local_engine(self) -> bool:
        return self.connection.local_engine

    def _show_rows(self) -> None:
        """Rows of a network service (address, key, service model): local speech recognition has none, the model is
        chosen in the "Choose a model" window."""
        if self._form is not None:
            for line in self._network_rows:
                self._form.setRowVisible(line, not self.local_engine)

    @property
    def chosen_model(self) -> str:
        return (self._local_model if self.local_engine else self._model) or self._preset().model

    def _fill(self) -> None:
        """Fields from the preset and the custom address; the service model list loads in the background."""
        self._generation += 1
        self._refresh.setEnabled(True)
        pr = self.connection
        self.address.setText(pr.address)
        self.address.setPlaceholderText(pr.example or tr("https://server/v1"))
        self._show_key()
        self._refresh.setVisible(bool(pr.models_endpoint))
        self._show_rows()
        if self.local_engine:
            return
        self._show_models(list(pr.models) or ([(pr.model, pr.model)] if pr.model else []), keep_draft=False)
        if pr.models_endpoint and pr.address:
            self._load_models()

    def _show_key(self) -> None:
        pr = self.connection
        self.secret.show_value(settings.address_key(pr.address, pr.key, pr.variable))
        self.secret.field.setPlaceholderText(tr("API key") if pr.key_needed else tr("If the server asks for one"))
        self.secret.result.hide()
        self.refresh_shared()

    @property
    def shared_key(self) -> bool:
        """The other section uses the same server, so the key is shared."""
        server = _server(self.connection.address)
        return bool(server) and server == _server(self._other[1]())

    def refresh_shared(self) -> None:
        """Marks that the key is shared with the other section: they have one server."""
        self.shared_label.setText(
            tr('This key is shared with "{section}": it\'s the same server', section=self._other[0])
        )
        self.shared_label.setVisible(self.shared_key)

    def _change(self, **fields: str) -> None:
        names = dict(zip(("preset", "address", "model"), FIELDS[self.function], strict=True))
        self.edited.emit({names[k]: z for k, z in fields.items()})

    def _change_preset(self) -> None:
        self._id, self._address, self._model = self.preset.currentData(), "", ""
        self._change(preset=self._id, address="", model="")
        self._fill()

    def _set_address(self) -> None:
        """The preset address is not stored in the settings: it updates with the program."""
        base = providers.base(self.address.text())
        custom_one = "" if base == self._preset().address else base
        self.address.setText(base or self._preset().address)
        if custom_one == self._address:
            return
        self._address = custom_one
        self._generation += 1
        self._change(address=custom_one)
        self._saved["адрес"].display()
        self._show_key()
        if self.connection.models_endpoint:
            self._load_models()

    def save_key(self) -> bool:
        """The key goes to the Windows credential store; if it was not written, the field keeps it and says why.
        True if the store holds the key in the field."""
        address = self.connection.address
        return self.secret.save(
            lambda key: settings.set_address_key(address, key),
            f"the key for {_server(address)}",
            tr("The Windows credential store did not save the key. Details are in the app log."),
        )

    def save_fields(self) -> None:
        """Unfinished address, key and model, before leaving the settings. The local model is chosen in its own window,
        not in a hidden field."""
        self._set_address()
        self.save_key()
        if not self.local_engine:
            self.set_model(self.model.currentText().rsplit(SEPARATOR, 1)[-1].strip())

    def _ask(self, work: Callable[[], object], answered: Callable[[object], None]) -> None:
        """Asks the service in the background; the answer comes to answered unless the preset or address changed."""
        generation = self._generation

        def arrived(result: object) -> None:
            if generation == self._generation:
                answered(result)
            else:
                log.info("an answer for an earlier preset or address was dropped")

        run_in_background(QThreadPool.globalInstance(), work, arrived)

    def _check(self) -> None:
        if not self.save_key():  # the line keeps saying why: meetings would not get the key checked
            return
        self.secret.report(tr("Checking…"), "тихий_текст")
        pr, key = self.connection, self.secret.field.text().strip()
        self._ask(lambda: checking(pr, key), self._check_result)

    def _check_result(self, result: object) -> None:
        if isinstance(result, Exception):
            self.secret.report(failures.text(result), "ошибка_текст")
            return
        error, models = result  # type: ignore[misc]
        if error:
            self.secret.report(error, "ошибка_текст")
            return
        accepted = tr("Key accepted") if self.secret.field.text().strip() else tr("Server responds")
        self.secret.report(tr("{result}, models: {n}", result=accepted, n=len(models)) if models else accepted, "успех")
        if models:
            self._show_models(models)

    def _load_models(self) -> None:
        pr, key = self.connection, self.secret.field.text().strip()
        self._refresh.setEnabled(False)
        self._ask(lambda: providers.models(pr, key), self._show_models)

    def _show_models(self, list_: object, *, keep_draft: bool = True) -> None:
        self._refresh.setEnabled(True)
        if isinstance(list_, Exception):
            self.secret.report(_list_failure(list_), "ошибка_текст")
            return
        field = self.model.lineEdit()
        draft = field.text() if keep_draft and field.isModified() else None
        cursor = field.cursorPosition()
        selection, length = field.selectionStart(), len(field.selectedText())
        current_one = self._model or self._preset().model
        self.model.clear()
        for id_, title in list_:  # type: ignore[attr-defined]
            self.model.addItem(id_ if title == id_ else f"{title}{SEPARATOR}{id_}", id_)
        self._pick_model(current_one)
        if draft is not None:
            self.model.setEditText(draft)
            if selection >= 0:
                field.setSelection(selection, length)
            else:
                field.setCursorPosition(cursor)
            field.setModified(True)
        field.setPlaceholderText(
            tr("Choose a model from the list") if self.model.count() else tr("Model name on the server")
        )

    def _pick_model(self, model: str) -> None:
        """A model from the list with its name, one typed in by hand as is."""
        number = self.model.findData(model)
        if number >= 0:
            self.model.setCurrentIndex(number)
        else:
            self.model.setEditText(model)

    def set_model(self, model: str) -> None:
        """The preset model is not stored in the settings: empty."""
        custom = "" if model == self._preset().model else model
        if self.local_engine:
            if custom != self._local_model:
                self._local_model = custom
                self.edited.emit({"local_model": custom})
            return
        self._pick_model(model)
        self.model.lineEdit().setModified(False)
        if custom != self._model:
            self._model = custom
            self._change(model=custom)
            self._saved["модель"].display()
