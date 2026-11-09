# SPDX-License-Identifier: GPL-3.0-only
"""Windows integration: autostart via HKCU Run and the launch command."""

import contextlib
import os
import pathlib
import tempfile
import unittest
import winreg
from unittest import mock

from knotes import failures, system

TRIAL = "Встречи-проверка-тестов"


class TestAutostart(unittest.TestCase):
    def tearDown(self):
        system.set_autostart(False, TRIAL)

    def test_enable_and_disable(self):
        self.assertFalse(system.autostart_enabled(TRIAL))
        system.set_autostart(True, TRIAL)
        self.assertTrue(system.autostart_enabled(TRIAL))
        system.set_autostart(False, TRIAL)
        system.set_autostart(False, TRIAL)
        self.assertFalse(system.autostart_enabled(TRIAL))

    def test_profile_without_run_key(self):
        """A fresh Windows profile has no Run key yet: turning autostart off does nothing, turning it on creates it."""
        parent, run = r"Software\QG Knotes tests", r"Software\QG Knotes tests\Run"
        for key in (run, parent):
            with contextlib.suppress(OSError):
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        try:
            with mock.patch.object(system, "AUTOSTART", run):
                system.set_autostart(False, TRIAL)
                self.assertFalse(system.autostart_enabled(TRIAL))
                system.set_autostart(True, TRIAL)
                self.assertTrue(system.autostart_enabled(TRIAL))
                system.set_autostart(False, TRIAL)
                self.assertFalse(system.autostart_enabled(TRIAL))
        finally:
            for key in (run, parent):
                with contextlib.suppress(OSError):
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)

    def test_old_russian_flags_keep_working(self):
        """Shortcuts and scripts made for earlier versions use Russian flags: they mean the English ones."""
        arguments = ["QG Knotes.exe", "--запись", "--настройки", "--расшифровать", "папка", "--заново", "--quit"]
        self.assertEqual(
            system.english_flags(arguments),
            ["QG Knotes.exe", "--record", "--settings", "--transcribe", "папка", "--again", "--quit"],
        )
        self.assertEqual(system.english_flags(["--перезапуск", "--локально"]), ["--restart", "--local-engine"])

    def test_key_combinations(self):
        self.assertEqual(system.windows_shortcut("Ctrl+Alt+R"), (0x3, 0x52))
        self.assertEqual(system.windows_shortcut("Ctrl+Shift+F9"), (0x6, 0x78))
        self.assertEqual(system.windows_shortcut("F8"), (0, 0x77))
        self.assertEqual(system.windows_shortcut("Meta+Space"), (0x8, 0x20))
        self.assertEqual(system.windows_shortcut("Alt+5"), (0x1, 0x35))
        for bad_value in ("", "R", "Ctrl+", "Ctrl+Ё", "Ctrl+Alt", "Мышь+R", "Ctrl+Unknown"):
            with self.subTest(bad_value=bad_value):
                self.assertIsNone(system.windows_shortcut(bad_value))

    def test_transcript_in_notepad_or_as_windows(self):
        path = pathlib.Path("расшифровка.md")
        notepad_patch = mock.patch.object(system.subprocess, "Popen")
        with notepad_patch as notepad, mock.patch.object(system.os, "startfile") as windows:
            system.open_text(path, True)
            system.open_text(path, False)
        program, file = notepad.call_args.args[0]
        self.assertEqual(file, "расшифровка.md")
        self.assertTrue(program.lower().endswith(r"\system32\notepad.exe"))  # Windows Notepad, not from the current dir
        windows.assert_called_once_with(path)

    def test_to_recycle_bin(self):
        path = pathlib.Path("C:/записи/встреча")
        with mock.patch.object(system.ctypes.windll.shell32, "SHFileOperationW", return_value=0) as call_:
            self.assertTrue(system.to_recycle_bin(path))
        action = call_.call_args.args[0]._obj
        self.assertEqual((action.wFunc, action.pFrom), (system.FO_DELETE, str(path)))
        self.assertTrue(action.fFlags & system.FOF_ALLOWUNDO)
        with mock.patch.object(system.ctypes.windll.shell32, "SHFileOperationW", return_value=2):
            self.assertFalse(system.to_recycle_bin(path))

    def test_processes_from_program_folder(self):
        """Before the update swaps the files: who else runs from the program folder (Claude Desktop's --mcp)."""
        paths = {
            10: r"C:\Programs\QG Knotes\QG Knotes.exe",
            11: r"c:\programs\qg knotes\_internal\helper.exe",
            12: r"C:\Programs\QG Knotes 2\QG Knotes.exe",
            13: "",
            os.getpid(): r"C:\Programs\QG Knotes\QG Knotes.exe",
        }
        with (
            mock.patch.object(system, "_pids", return_value=list(paths)),
            mock.patch.object(system, "process_path", paths.get),
        ):
            self.assertEqual(system.processes_in(pathlib.Path(r"C:\Programs\QG Knotes")), [10, 11])
        self.assertEqual(system.processes_in(pathlib.Path(tempfile.gettempdir()) / "нет такой папки"), [])

    def test_ffmpeg_missing_in_plain_text(self):
        import imageio_ffmpeg  # the module system.ffmpeg() loads on demand

        with (
            mock.patch.object(imageio_ffmpeg, "get_ffmpeg_exe", side_effect=RuntimeError("No ffmpeg exe found")),
            self.assertLogs(system.log, "ERROR"),
            self.assertRaises(system.FfmpegMissing) as error,
        ):
            system.ffmpeg()
        self.assertEqual(
            failures.text(error.exception),
            "ffmpeg не найден: переустановите программу или проверьте карантин антивируса.",
        )

    def test_command_in_development(self):
        command = system.launch_command()
        self.assertIn("pythonw.exe", command)
        self.assertTrue(command.endswith("-m knotes --autostart"))


class TestBringToFront(unittest.TestCase):
    def test_maximized_window_stays_maximized(self):
        """Opening the settings or a meeting brings the window forward: a maximized window stays maximized, a
        minimized one comes back as it was before."""
        from PySide6.QtWidgets import QApplication, QWidget  # noqa: PLC0415 - only this test needs windows

        app = QApplication.instance() or QApplication([])
        window = QWidget()
        window.showMaximized()
        app.processEvents()
        system.bring_to_front(window)
        app.processEvents()
        self.assertTrue(window.isMaximized())
        window.showMinimized()
        app.processEvents()
        system.bring_to_front(window)
        app.processEvents()
        self.assertEqual((window.isMinimized(), window.isMaximized()), (False, True))
        window.close()


if __name__ == "__main__":
    unittest.main()
