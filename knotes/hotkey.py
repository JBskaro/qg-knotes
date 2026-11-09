# SPDX-License-Identifier: GPL-3.0-only
"""A shortcut from the settings that works in any program, even with the program windows hidden: RegisterHotKey of
Windows, its WM_HOTKEY message caught in the native events of the window."""

import ctypes
import ctypes.wintypes
import logging

from PySide6.QtCore import QAbstractNativeEventFilter, QTimer

from knotes import system

log = logging.getLogger(__name__)
WM_HOTKEY, MOD_NOREPEAT = 0x0312, 0x4000


class Hotkey(QAbstractNativeEventFilter):
    """A shortcut from the settings via RegisterHotKey: works even when the program windows are hidden."""

    def __init__(self, window: int, id_: int, action: object):
        super().__init__()
        self.window, self.id_, self.action, self.shortcut = window, id_, action, ""

    def enable(self, shortcut: str) -> bool:
        """Replaces the shortcut; False: Windows did not grant it (taken by another program or not valid)."""
        if shortcut == self.shortcut:
            return True
        if self.shortcut:
            ctypes.windll.user32.UnregisterHotKey(self.window, self.id_)
            self.shortcut = ""
        key_seq = system.windows_shortcut(shortcut) if shortcut else None
        if key_seq is None:
            return not shortcut
        modifiers, code = key_seq
        if not ctypes.windll.user32.RegisterHotKey(self.window, self.id_, modifiers | MOD_NOREPEAT, code):
            log.warning("shortcut %s is taken by another program", shortcut)
            return False
        self.shortcut = shortcut
        return True

    def nativeEventFilter(self, kind: bytes, message: int) -> tuple[bool, int]:
        if kind == b"windows_generic_MSG":
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == self.id_:
                QTimer.singleShot(0, self.action)
                return True, 0
        return False, 0
