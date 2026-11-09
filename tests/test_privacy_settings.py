# SPDX-License-Identifier: GPL-3.0-only
"""The Minutes settings: switches for hiding personal data and names from a cloud model."""

import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from knotes import language, providers, settings
from knotes.ui import settings_window, style

program = QApplication.instance() or QApplication([])


class TestPrivacySwitches(unittest.TestCase):
    def setUp(self):
        for patcher in (
            mock.patch.object(settings, "key", return_value=""),
            mock.patch.object(settings, "address_key", return_value=""),
            mock.patch.object(settings, "set_address_key"),
            mock.patch.object(providers, "models", return_value=[]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        language.set_("ru")
        self.window = settings_window.SettingsWindow(settings.Settings())
        self.addCleanup(self.window.deleteLater)
        self.changes: list[settings.Settings] = []
        self.window.changed.connect(self.changes.append)

    def test_switches_save_and_names_need_the_first(self):
        hide = self.window.findChild(style.Switch, "скрывать_личные_данные")
        names = self.window.findChild(style.Switch, "скрывать_имена")
        self.assertEqual((hide.isChecked(), names.isChecked(), names.isEnabled()), (True, False, True))
        names.click()
        self.assertTrue(self.changes[-1].hide_names)
        hide.click()
        self.assertFalse(self.changes[-1].hide_personal_data)
        self.assertFalse(names.isEnabled())


if __name__ == "__main__":
    unittest.main()
