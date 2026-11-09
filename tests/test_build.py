# SPDX-License-Identifier: GPL-3.0-only
"""Installing a new build: the program folder is replaced whole or not at all."""

import pathlib
import tempfile
import unittest
from unittest import mock

import build


class TestInstall(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        root = pathlib.Path(self._temporary.name)
        self.new_build, self.program = root / "готово", root / "QG Knotes"
        self.new_build.mkdir()
        (self.new_build / "QG Knotes.exe").write_text("новая", encoding="utf-8")
        self.program.mkdir()
        (self.program / "QG Knotes.exe").write_text("старая", encoding="utf-8")
        (self.program / "лишний.dll").write_text("x", encoding="utf-8")

    def _exe(self) -> str:
        return (self.program / "QG Knotes.exe").read_text(encoding="utf-8")

    def test_program_that_does_not_exit_keeps_its_folder(self):
        """A program waiting for an answer to "Stop the recording?" keeps running: nothing of it is deleted."""
        with mock.patch.object(build, "_close_running", return_value=False), mock.patch("builtins.print"):
            self.assertFalse(build.install(self.new_build, self.program))
        self.assertEqual(self._exe(), "старая")
        self.assertTrue((self.program / "лишний.dll").exists())

    def test_new_build_takes_the_place_of_the_old_one(self):
        with mock.patch.object(build, "_close_running", return_value=True):
            self.assertTrue(build.install(self.new_build, self.program))
        self.assertEqual(self._exe(), "новая")
        self.assertFalse((self.program / "лишний.dll").exists())
        self.assertEqual(sorted(p.name for p in self.program.parent.iterdir()), ["QG Knotes", "готово"])

    def test_files_still_held_stop_the_install(self):
        with (
            mock.patch.object(build, "_close_running", return_value=True),
            mock.patch.object(build, "RENAME_TRIES", 1),
            mock.patch.object(build.time, "sleep"),
            mock.patch.object(pathlib.Path, "rename", side_effect=PermissionError("занят")),
            mock.patch("builtins.print"),
        ):
            self.assertFalse(build.install(self.new_build, self.program))
        self.assertEqual(self._exe(), "старая")

    def test_new_build_that_cannot_take_the_place_puts_the_old_one_back(self):
        """An antivirus still scanning the new files: the old program comes back, shortcuts keep working."""
        real = pathlib.Path.rename

        def rename(source: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
            if source.name.endswith(".новая"):
                raise PermissionError("проверяет антивирус")
            return real(source, target)

        with (
            mock.patch.object(build, "_close_running", return_value=True),
            mock.patch.object(build, "RENAME_TRIES", 2),
            mock.patch.object(build.time, "sleep"),
            mock.patch.object(pathlib.Path, "rename", rename),
            mock.patch("builtins.print"),
        ):
            self.assertFalse(build.install(self.new_build, self.program))
        self.assertEqual(self._exe(), "старая")
        self.assertTrue((self.program.parent / "QG Knotes.новая").exists())

    def test_waits_for_the_program_to_let_go(self):
        with mock.patch.object(build.subprocess, "run") as quit_, mock.patch.object(build, "QLockFile") as lock:
            lock.return_value.tryLock.return_value = False
            self.assertFalse(build._close_running(self.program, "QG Knotes.exe"))
            quit_.assert_called_once()
            lock.return_value.tryLock.return_value = True
            self.assertTrue(build._close_running(self.program, "QG Knotes.exe"))
        self.assertTrue(build._close_running(self.new_build.parent / "нет", "QG Knotes.exe"))


if __name__ == "__main__":
    unittest.main()
