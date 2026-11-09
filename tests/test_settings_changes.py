# SPDX-License-Identifier: GPL-3.0-only
"""Settings changed in a window with its own copy: only its changes are applied, the rest stays as it is now."""

import dataclasses
import unittest

from knotes import settings, settings_changes


class TestEdits(unittest.TestCase):
    def test_only_what_the_window_changed(self):
        start = settings.Settings(projects=["Склад"], theme="система")
        edits = settings_changes.Edits(start)
        window_copy = dataclasses.replace(start)
        current = dataclasses.replace(start, projects=["Остатки"], bar_collapsed=True, updates_checked="2026-10-27")
        window_copy = dataclasses.replace(window_copy, theme="темная")
        current = edits.apply(current, window_copy)
        self.assertEqual(
            (current.theme, current.projects, current.bar_collapsed, current.updates_checked),
            ("темная", ["Остатки"], True, "2026-10-27"),
        )
        window_copy = dataclasses.replace(window_copy, hotkey="Ctrl+Alt+Q")  # the theme is not sent again as new
        current = edits.apply(dataclasses.replace(current, theme="светлая"), window_copy)
        self.assertEqual((current.theme, current.hotkey), ("светлая", "Ctrl+Alt+Q"))

    def test_a_list_changed_in_place_in_the_window(self):
        start = settings.Settings(meeting_apps=["Zoom.exe"])
        edits = settings_changes.Edits(start)
        start.meeting_apps.append("Teams.exe")  # a window that edits the list it was given
        current = edits.apply(settings.Settings(meeting_apps=["Zoom.exe"]), start)
        self.assertEqual(current.meeting_apps, ["Zoom.exe", "Teams.exe"])
        self.assertIsNot(current.meeting_apps, start.meeting_apps)


if __name__ == "__main__":
    unittest.main()
