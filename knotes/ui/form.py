# SPDX-License-Identifier: GPL-3.0-only
"""Settings form rows: a setting caption with a tooltip, widgets in a row and in a column, the "Saved" mark, the
device list of the settings and of the first-run setup, a write to the Windows credential store or registry, a
secret field kept there."""

import logging
from collections.abc import Callable

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from knotes.language import tr, tr_key
from knotes.ui import style

log = logging.getLogger(__name__)
CAPTION_WIDTH = 210
VISIBLE_MS = 2500  # how long the "Saved" mark stays visible
AS_IN_WINDOWS = tr_key("Как в Windows")


class SavedMark(QLabel):
    """Shows "✓ Saved" next to a text field for a couple of seconds after saving: there is no "Save" button, so
    this tells that nothing needs confirming. The space for the mark is kept while it is hidden: the field does not
    jump."""

    def __init__(self, parent_: QWidget | None = None):
        super().__init__("✓ " + tr("Saved"), parent_)
        self.setObjectName("успех")
        policy = self.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.setSizePolicy(policy)
        self._timer = QTimer(self, singleShot=True, interval=VISIBLE_MS)
        self._timer.timeout.connect(self.hide)
        self.hide()

    def display(self) -> None:
        self.show()
        self._timer.start()


def row_label(text: str, hint: str = "") -> QLabel:
    """Setting caption; what the setting does goes into the tooltip."""
    label = QLabel(text)
    label.setFixedWidth(CAPTION_WIDTH)
    label.setWordWrap(True)
    if hint:
        label.setToolTip(f"<p style='white-space: normal; width: 300px'>{hint}</p>")
    return label


def row(*widgets: QWidget, stretch: bool = False) -> QWidget:
    result = QWidget()
    window_layout = QHBoxLayout(result)
    window_layout.setContentsMargins(0, 0, 0, 0)
    for widget in widgets:
        window_layout.addWidget(widget)
    if not stretch:
        window_layout.addStretch()
    return result


def column(*widgets: QWidget) -> QWidget:
    result = QWidget()
    window_layout = QVBoxLayout(result)
    window_layout.setContentsMargins(0, 0, 0, 0)
    for widget in widgets:
        window_layout.addWidget(widget)
    return result


def devices(names: list[str], current: str) -> QComboBox:
    """A device list: "As in Windows" and the connected ones; one chosen before but now disconnected is marked, so the
    choice is not lost while it is away."""
    list_ = QComboBox()
    list_.addItem(tr(AS_IN_WINDOWS), "")
    for name in names:
        list_.addItem(name, name)
    if current and current not in names:
        list_.addItem(tr("{device} (not connected)", device=current), current)
    list_.setCurrentIndex(max(0, list_.findData(current)))
    return list_


def stored(action: Callable[[], object], what: str) -> bool:
    """Writes to the Windows credential store or the registry; a failure goes to the log, and the setting says it was
    not saved. what names the setting in the log, never its value. True if written."""
    try:
        action()
    except Exception:  # noqa: BLE001 - besides OSError the credential store raises its own pywintypes.error
        log.warning("%s was not saved", what, exc_info=True)
        return False
    return True


class SecretField:
    """A secret kept in the Windows credential store, an API key or the calendar link: hidden on screen like a
    password, "Check" next to it and a line under it with the check result or why the secret was not saved."""

    def __init__(self, check_tooltip: str, parent_: QWidget | None = None):
        self.field = QLineEdit(parent_)
        self.field.setEchoMode(QLineEdit.EchoMode.Password)
        self.saved = SavedMark(parent_)
        self.check = QPushButton(tr("Check"), parent_)
        self.check.setToolTip(check_tooltip)
        self.result = QLabel(parent_)
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.TextFormat.PlainText)  # the server answer as text, not markup
        self.result.hide()
        self.value = ""  # what the store holds: the field is compared with it

    def show_value(self, value: str) -> None:
        """The stored secret, read again (the address changed)."""
        self.value = value
        self.field.setText(value)

    def save(self, store: Callable[[str], object], what: str, refused: str) -> bool:
        """The field to the store, if it changed; if it was not written, the field keeps it and the line says refused.
        what names the secret in the log, never its value. True if the store holds what the field shows."""
        text = self.field.text().strip()
        if text == self.value:
            return True
        if not stored(lambda: store(text), what):
            self.report(refused, "ошибка_текст")
            return False
        self.value = text
        self.saved.display()
        return True

    def report(self, text: str, kind: str) -> None:
        self.result.setText(text)
        style.restyle(self.result, kind)
        self.result.show()
