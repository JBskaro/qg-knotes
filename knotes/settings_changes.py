# SPDX-License-Identifier: GPL-3.0-only
"""Changes from a window that edits its own copy of the settings (the settings section, the setup).

Such a window sends its whole copy after every change. Only the fields it changed are applied: what changed elsewhere
meanwhile (a project renamed on the left, its color, the recording bar collapsed, the window layout, the date of the
update check) is not rolled back to the window's old copy.
"""

import copy
import dataclasses

from knotes import settings


class Edits:
    """The window's copy as it was last seen: the settings when it opened, then each copy it sent."""

    def __init__(self, start: settings.Settings):
        self._seen = copy.deepcopy(start)

    def apply(self, current: settings.Settings, sent: settings.Settings) -> settings.Settings:
        """The current settings with the fields the window changed since it was last seen."""
        changed = {
            field.name: copy.deepcopy(getattr(sent, field.name))
            for field in dataclasses.fields(sent)
            if getattr(sent, field.name) != getattr(self._seen, field.name)
        }
        self._seen = copy.deepcopy(sent)
        return dataclasses.replace(current, **changed)
