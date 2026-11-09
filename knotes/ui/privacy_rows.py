# SPDX-License-Identifier: GPL-3.0-only
"""Rows of the Minutes settings about personal data: hide it from a cloud model, and people's names too."""

from collections.abc import Callable

from PySide6.QtWidgets import QFormLayout

from knotes.language import tr
from knotes.ui import style
from knotes.ui.form import row, row_label


def add(form_layout: QFormLayout, switch: Callable[[str], style.Switch]) -> None:
    """switch(field) makes a switch that saves the settings field; the names one works only with the first on."""
    hide, names = switch("hide_personal_data"), switch("hide_names")
    hide.setObjectName("скрывать_личные_данные")
    names.setObjectName("скрывать_имена")
    form_layout.addRow(
        row_label(
            tr("Hide personal data from the cloud model"),
            tr(
                "Phones, e-mails, INN, SNILS, passports, card and account numbers and addresses go to a cloud minutes "
                "model and to your AI assistant as labels like [PHONE_1]; the minutes on disk get the real values "
                "back. A model on this computer or in your network gets the text as is. Audio sent to cloud speech "
                "recognition can't be hidden."
            ),
        ),
        row(hide),
    )
    form_layout.addRow(
        row_label(
            tr("Hide people's names too"),
            tr(
                "Names of participants and speakers and surnames from the vocabulary become labels too. The model "
                "then writes full names and task assignees worse: names come back in the nominative case."
            ),
        ),
        row(names),
    )
    names.setEnabled(hide.isChecked())
    hide.toggled.connect(names.setEnabled)
