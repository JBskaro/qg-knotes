# SPDX-License-Identifier: GPL-3.0-only
"""The app log: the running copy rotates its file and keeps writing when Windows holds it; side processes write files
of their own; uncaught errors of any thread reach the log and the toast."""

import logging
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest import mock

from knotes import journal


class TestJournal(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self._temporary.name) / "QG Knotes"
        patch = mock.patch.object(journal.settings, "log_folder", return_value=self.folder)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self._temporary.cleanup)

    def _setup(self, side: str = "") -> logging.Handler:
        handler = journal.setup(side)
        self.addCleanup(handler.close)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        return handler

    def test_side_processes_write_their_own_files(self):
        """The MCP server and command line transcription do not write the log of the running copy."""
        self._setup("mcp")
        self._setup("transcribe")
        logging.getLogger("проверка").warning("строка")
        self.assertIn("строка", (self.folder / "журнал_mcp.log").read_text(encoding="utf-8"))
        self.assertIn("строка", (self.folder / "журнал_распознавания.log").read_text(encoding="utf-8"))
        self.assertFalse((self.folder / journal.FILE).exists())
        self.assertEqual(journal.path(), self.folder / "журнал.log")

    def test_side_log_set_aside_when_big_and_kept_when_held(self):
        self.folder.mkdir()
        big = self.folder / "журнал_mcp.log"
        big.write_text("x" * (journal.LIMIT + 1), encoding="utf-8")
        self._setup("mcp").close()
        self.assertTrue((self.folder / "журнал_mcp.log.1").exists())
        big.write_text("x" * (journal.LIMIT + 1), encoding="utf-8")
        with mock.patch.object(journal.os, "replace", side_effect=PermissionError("занят другой копией")):
            handler = self._setup("mcp")
        handler.emit(logging.LogRecord("п", logging.WARNING, "", 0, "дальше", None, None))
        handler.flush()
        self.assertTrue(big.read_text(encoding="utf-8").endswith("дальше\n"))

    def test_rotation_held_by_windows_does_not_stop_the_log(self):
        """Windows does not let a file held by another program be renamed: the record is still written, the next
        rotation is tried later, not on every line."""
        handler = self._setup()
        record = logging.LogRecord("п", logging.WARNING, "", 0, "x" * 600, None, None)
        handler.maxBytes = 1000
        with mock.patch.object(handler, "rotate", side_effect=PermissionError("занят")) as rotate:
            for _ in range(5):
                handler.emit(record)
        handler.flush()
        self.assertEqual(rotate.call_count, 1)
        self.assertEqual((self.folder / journal.FILE).read_text(encoding="utf-8").count("x" * 600), 5)

    def test_uncaught_errors_of_any_thread_logged_and_told(self):
        hooks = (sys.excepthook, threading.excepthook)
        self.addCleanup(lambda: (setattr(sys, "excepthook", hooks[0]), setattr(threading, "excepthook", hooks[1])))
        told: list[BaseException] = []
        journal.catch_uncaught()
        journal.listen(told.append)
        self.addCleanup(journal.listen, None)
        thread = threading.Thread(target=lambda: 1 / 0, name="фон")
        with self.assertLogs(journal.log, "ERROR") as logged:
            thread.start()
            thread.join()
            try:
                raise ValueError("в окне")
            except ValueError as error:
                sys.excepthook(type(error), error, error.__traceback__)
        self.assertIn("фон", logged.output[0])
        self.assertEqual([type(e) for e in told], [ZeroDivisionError, ValueError])

    def test_listener_gone_with_the_program(self):
        def closed(_error: BaseException) -> None:
            raise RuntimeError("Internal C++ object already deleted")

        journal.listen(closed)
        self.addCleanup(journal.listen, None)
        with self.assertLogs(journal.log, "INFO"):
            journal._tell(ValueError("x"))

    def test_log_folder_that_cannot_be_made(self):
        with (
            mock.patch.object(pathlib.Path, "mkdir", side_effect=PermissionError("нет доступа")),
            mock.patch.object(journal.logging, "basicConfig") as fallback,
        ):
            self.assertIsNone(journal.setup())
        fallback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
