# SPDX-License-Identifier: GPL-3.0-only
"""A shortcut that works in any program: granted by Windows or reported as taken, replaced, its message handled."""

import ctypes
import ctypes.wintypes
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from knotes import hotkey

qt = QApplication.instance() or QApplication([])


class TestHotkey(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(hotkey.ctypes, "windll")  # no shortcut of this computer is taken by the tests
        self.user32 = patch.start().user32
        self.addCleanup(patch.stop)
        self.calls: list[bool] = []
        self.key = hotkey.Hotkey(7, 1, lambda: self.calls.append(True))

    def test_granted_replaced_and_taken(self):
        self.user32.RegisterHotKey.return_value = 1
        self.assertTrue(self.key.enable("Ctrl+Alt+R"))
        self.assertEqual(self.key.shortcut, "Ctrl+Alt+R")
        self.assertTrue(self.key.enable("Ctrl+Alt+R"))  # the same one: Windows is not asked again
        self.assertEqual(self.user32.RegisterHotKey.call_count, 1)
        self.user32.RegisterHotKey.return_value = 0
        with self.assertLogs(hotkey.log, "WARNING"):
            self.assertFalse(self.key.enable("Ctrl+Alt+M"))  # taken by another program
        self.user32.UnregisterHotKey.assert_called_once_with(7, 1)
        self.assertEqual(self.key.shortcut, "")
        self.assertFalse(self.key.enable("R"))  # not usable: a letter without a modifier would stop typing
        self.assertTrue(self.key.enable(""))  # off

    def test_message_of_this_shortcut_runs_its_action(self):
        message = ctypes.wintypes.MSG()
        message.message, message.wParam = hotkey.WM_HOTKEY, 1
        self.assertEqual(self.key.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(message)), (True, 0))
        message.wParam = 2  # another shortcut of the program
        self.assertEqual(self.key.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(message)), (False, 0))
        self.assertEqual(self.key.nativeEventFilter(b"xcb_generic_event_t", 0), (False, 0))
        qt.processEvents()
        self.assertEqual(self.calls, [True])


if __name__ == "__main__":
    unittest.main()
