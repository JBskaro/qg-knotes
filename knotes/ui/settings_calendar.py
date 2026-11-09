# SPDX-License-Identifier: GPL-3.0-only
"""The calendar link in the settings: a secret kept in the Windows credential store like a key; "Check" saves it and
shows the upcoming meetings from the calendar."""

from PySide6.QtCore import QThreadPool, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from knotes import failures, ics_calendar, providers, settings
from knotes.language import tr
from knotes.ui.background import run_in_background
from knotes.ui.form import SecretField, row


class CalendarField(QWidget):
    """The link field with "Check" and, under it, what the check found or why the link was not saved."""

    _checked = Signal(object)

    def __init__(self):
        super().__init__()
        self._checked.connect(self._check_result)
        self.secret = SecretField(tr("Open the calendar by the link and show upcoming meetings"))
        self.secret.show_value(settings.key(ics_calendar.KEY))
        self.secret.field.setPlaceholderText(tr("https://... .ics, empty - no calendar"))
        self.secret.field.editingFinished.connect(self.save)
        self.secret.check.clicked.connect(self._check)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(row(self.secret.field, self.secret.check, self.secret.saved, stretch=True))
        column.addWidget(self.secret.result)

    def save(self) -> bool:
        """The link goes to the Windows credential store; if it was not written, the field keeps it and says why.
        True if the store holds the link in the field."""
        return self.secret.save(
            lambda url: settings.set_key(ics_calendar.KEY, url),
            "the calendar link",
            tr("The Windows credential store did not save the link. Details are in the app log."),
        )

    def _check(self) -> None:
        if not self.save():  # the line keeps saying why: the stored link is not the one in the field
            return
        url = self.secret.value
        if not url:
            self._check_result(providers.ProviderError(tr("No link: paste an ICS link")))
            return
        self.secret.report(tr("Checking…"), "тихий_текст")
        run_in_background(QThreadPool.globalInstance(), lambda: ics_calendar.upcoming(url), self._checked.emit)

    def _check_result(self, result: object) -> None:
        if isinstance(result, Exception):
            self.secret.report(failures.text(result), "ошибка_текст")
            return
        meetings: list[ics_calendar.Event] = result  # type: ignore[assignment]
        if not meetings:
            self.secret.report(tr("The calendar opened, no meetings this week"), "успех")
            return
        first = meetings[0]
        self.secret.report(
            tr(
                "The calendar opened, meetings this week: {n}. Next: {title}, {when}",
                n=len(meetings),
                title=first.title,
                when=f"{first.start:%d.%m %H:%M}",
            ),
            "успех",
        )
