# SPDX-License-Identifier: GPL-3.0-only
"""What a settings section gets from the window: the settings being edited, widgets bound to their fields (a change
is written at once, there is no "Save" button), buttons with an icon of the text color; the section page itself."""

import dataclasses
from collections.abc import Callable, Sequence

from PySide6.QtWidgets import QComboBox, QFormLayout, QPushButton, QWidget

from knotes import settings
from knotes.language import tr
from knotes.ui import style


def labels_text(variants: Sequence[tuple[object, str]]) -> list[tuple[object, str]]:
    """Options with labels in the interface language."""
    return [(value, tr(row_label)) for value, row_label in variants]


def listing(variants: Sequence[tuple[object, str]], current: object) -> QComboBox:
    field = QComboBox()
    for value, row_label in variants:
        field.addItem(row_label, value)
    field.setCurrentIndex(max(0, field.findData(current)))
    return field


class Editor:
    """The settings being edited; changed(settings) after each change."""

    def __init__(self, config: settings.Settings, changed: Callable[[settings.Settings], None]):
        self.settings = dataclasses.replace(config)
        self._changed = changed
        self.icons: list[tuple[object, str]] = []  # what has an icon of the text color, and its sign

    def change(self, **fields: object) -> None:
        self.settings = dataclasses.replace(self.settings, **fields)
        self._changed(self.settings)

    def switch(self, field: str) -> style.Switch:
        switch = style.Switch(bool(getattr(self.settings, field)))
        switch.toggled.connect(lambda on: self.change(**{field: on}))
        return switch

    def choice(self, variants: Sequence[tuple[object, str]], field: str) -> QComboBox:
        list_ = listing(variants, getattr(self.settings, field))
        list_.setMinimumWidth(260)
        list_.currentIndexChanged.connect(lambda: self.change(**{field: list_.currentData()}))
        return list_

    def segments(self, variants: Sequence[tuple[object, str]], field: str) -> style.Segments:
        segmented = style.Segments(variants, getattr(self.settings, field))
        segmented.selected.connect(lambda value: self.change(**{field: value}))
        return segmented

    def with_icon(self, sign: str, text: str = "") -> QPushButton:
        """A button with an icon of the text color: drawn again when the theme changes."""
        button = QPushButton(text)
        self.icons.append((button, sign))
        return button


class Page(QWidget):
    """A settings section: captions with a tooltip on the left, fields on the right."""

    def __init__(self, editor: Editor):
        super().__init__()
        self.editor = editor
        self.form_layout = QFormLayout(self)
        self.form_layout.setContentsMargins(20, 14, 16, 8)
        self.form_layout.setVerticalSpacing(12)
        self.form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
