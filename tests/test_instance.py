# SPDX-License-Identifier: GPL-3.0-only
"""One copy per user: a second start passes its command, an unknown command is ignored, only the same user can
connect, a copy that is exiting lets the new one start."""

import os
import subprocess
import sys
import threading
import time
import unittest
import uuid
from unittest import mock

from PySide6.QtCore import QLockFile
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import QApplication

from knotes import instance

app = QApplication.instance() or QApplication([])
SECOND_COPY = (
    "import sys; from PySide6.QtCore import QCoreApplication; from knotes import instance; "
    "qt = QCoreApplication([]); name = sys.argv[1]; print({call})"
)


def wait_for(condition: object, seconds: float = 3.0) -> None:
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


class TestInstance(unittest.TestCase):
    def setUp(self):
        self.name = f"qg-knotes-test-{uuid.uuid4().hex[:8]}"
        self.done: list[str] = []
        actions = {instance.SHOW: lambda: self.done.append("окно"), instance.QUIT: lambda: self.done.append("выход")}
        self.server = instance.Server(actions, self.name)
        self.addCleanup(self.server.close)

    def test_flags_to_commands(self):
        self.assertEqual(instance.command(["x"]), instance.SHOW)
        self.assertEqual(instance.command(["x", "--record"]), instance.TOGGLE)
        self.assertEqual(instance.command(["x", "--settings"]), instance.SETTINGS)
        self.assertEqual(instance.command(["x", "--quit"]), instance.QUIT)

    def _another_copy(self, call: str, busy_s: float = 0.0) -> str:
        """What a second copy of the program prints: it is another process, as in real life (a thread of this one
        would hold the interpreter lock while it waits, and this copy could not answer). busy_s: this copy handles
        no events for that long, as when the window is busy."""
        child = subprocess.Popen(
            [sys.executable, "-c", SECOND_COPY.format(call=call), self.name],
            stdout=subprocess.PIPE,
            text=True,
            env=os.environ | {"PYTHONPATH": os.pathsep.join(sys.path)},
        )
        time.sleep(busy_s)
        wait_for(lambda: child.poll() is not None, 20)
        return child.communicate(timeout=5)[0].strip()

    def test_busy_running_copy_still_gets_the_command(self):
        self.assertEqual(self._another_copy("instance.send(instance.QUIT, name)", busy_s=2.0), "True")
        wait_for(lambda: self.done)
        self.assertEqual(self.done, ["выход"])

    def test_second_start_passes_its_command(self):
        self.assertTrue(self.server.listening)
        self.assertEqual(self._another_copy("instance.send(instance.QUIT, name)"), "True")
        wait_for(lambda: self.done)
        self.assertEqual(self.done, ["выход"])

    def test_unknown_command_does_not_open_the_window(self):
        with self.assertLogs(instance.log, "WARNING"):
            self.assertEqual(self._another_copy("instance.send(b'open-everything', name)"), "True")
            wait_for(lambda: False, 0.3)
        self.assertEqual(self.done, [])

    def test_only_the_same_user_connects(self):
        option = self.server._server.socketOptions()
        self.assertTrue(option & QLocalServer.SocketOption.UserAccessOption)

    def test_closed_server_takes_no_commands(self):
        self.server.close()
        self.assertFalse(instance.send(instance.SHOW, self.name))

    def test_exiting_copy_lets_the_new_one_start(self):
        """The exiting copy has closed its server but still saves the recording; the new start takes the lock once
        it is let go, instead of giving up."""
        self.server.close()
        held = QLockFile(str(instance.lock_path(self.name)))
        self.assertTrue(held.tryLock(0))
        self.addCleanup(lambda: instance.lock_path(self.name).unlink(missing_ok=True))
        threading.Timer(0.3, held.unlock).start()
        with mock.patch.object(instance, "WAIT_FIRST_S", 3):
            lock = instance.first(instance.SHOW, 0, self.name)
        self.assertIsNotNone(lock)
        lock.unlock()

    def test_running_copy_gets_the_command(self):
        held = QLockFile(str(instance.lock_path(self.name)))
        self.assertTrue(held.tryLock(0))
        self.addCleanup(held.unlock)
        self.assertEqual(self._another_copy("instance.first(instance.SHOW, 0, name)"), "None")
        wait_for(lambda: self.done)
        self.assertEqual(self.done, ["окно"])

    def test_nobody_answers(self):
        held = QLockFile(str(instance.lock_path(self.name)))
        self.assertTrue(held.tryLock(0))
        self.addCleanup(held.unlock)
        self.server.close()
        with mock.patch.object(instance, "WAIT_FIRST_S", 0), self.assertLogs(instance.log, "WARNING"):
            self.assertIsNone(instance.first(instance.SHOW, 0, self.name))
        self.assertTrue(os.path.basename(instance.lock_path()).startswith("qg-knotes-"))


if __name__ == "__main__":
    unittest.main()
