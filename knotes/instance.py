# SPDX-License-Identifier: GPL-3.0-only
"""One copy of the program per user: a second start passes its command to the running copy and exits.

The running copy listens on a local server that only the same user can connect to, and reads a command when it
arrives, without holding up the windows. The lock file keeps a second copy from starting while the first one is still
starting (the first start after install can take long: Windows checks the new files) or is still exiting.
"""

import ctypes
import getpass
import logging
import pathlib
from collections.abc import Callable

from PySide6.QtCore import QDir, QLockFile, QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

SERVER = f"qg-knotes-{getpass.getuser()}"
SHOW, QUIT, TOGGLE, SETTINGS = b"show", b"quit", b"toggle", b"settings"
FLAGS = {"--quit": QUIT, "--record": TOGGLE, "--settings": SETTINGS}
WAIT_FIRST_S = 40  # how long a second start waits while the first copy starts or exits
WAIT_EXIT_MS = 20_000  # on restart: how long to wait for the previous copy to exit
LONGEST = 16  # bytes: anything longer is not a command
HOLD_MS = 5000  # how long a second start keeps the line open for a busy running copy
log = logging.getLogger(__name__)


def command(arguments: list[str]) -> bytes:
    return next((code for flag, code in FLAGS.items() if flag in arguments), SHOW)


def lock_path(server: str = SERVER) -> pathlib.Path:
    return pathlib.Path(QDir.tempPath()) / f"{server}.lock"


def send(code: bytes, server: str = SERVER) -> bool:
    """Passes the command to the running copy; False: no copy is listening."""
    link = QLocalSocket()
    link.connectToServer(server)
    if not link.waitForConnected(300):
        return False
    ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY: let the running copy come to the front
    link.write(code)
    link.waitForBytesWritten(300)
    if not link.waitForDisconnected(HOLD_MS):  # the running copy hangs up once it has read: leaving earlier
        link.disconnectFromServer()  # loses the command while that copy is busy
    return True


def first(code: bytes, wait_ms: int = 0, server: str = SERVER) -> QLockFile | None:
    """The lock of the program copy; None: another copy is running and got the command. A copy that is exiting
    has closed its server already: the lock is taken as soon as it lets go."""
    lock = QLockFile(str(lock_path(server)))
    if lock.tryLock(wait_ms):
        return lock
    for _ in range(WAIT_FIRST_S * 2):
        if send(code, server):
            return None
        if lock.tryLock(500):
            return lock
    log.warning("another copy holds the lock and does not answer")
    return None


class Server(QObject):
    """Commands of the copies started later; an unknown one is ignored."""

    def __init__(self, actions: dict[bytes, Callable[[], object]], name: str = SERVER):
        super().__init__()
        self._actions = actions
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)  # other users of the computer: no
        self._server.newConnection.connect(self._accept)
        QLocalServer.removeServer(name)  # left by a copy that crashed; the lock says no other copy runs now
        self.listening = self._server.listen(name)
        if not self.listening:
            log.warning("command server %s did not start: %s", name, self._server.errorString())

    def close(self) -> None:
        """A copy that is exiting takes no more commands: a new start does not talk to it."""
        self._server.close()

    def _accept(self) -> None:
        while (link := self._server.nextPendingConnection()) is not None:
            received = bytearray()
            link.readyRead.connect(lambda s=link, r=received: self._read(s, r))
            link.disconnected.connect(lambda s=link, r=received: self._gone(s, r))
            if link.bytesAvailable():
                self._read(link, received)

    def _read(self, link: QLocalSocket, received: bytearray) -> None:
        received += bytes(link.readAll().data())
        action = self._actions.get(bytes(received))
        if action is None and len(received) <= LONGEST:
            return  # the rest of the command may be on its way
        if action is None:
            log.warning("unknown command from another copy: %r", bytes(received[:LONGEST]))
        received.clear()
        link.disconnectFromServer()
        if action is not None:
            action()

    def _gone(self, link: QLocalSocket, received: bytearray) -> None:
        if link.bytesAvailable():
            self._read(link, received)
        if received:
            log.warning("unknown command from another copy: %r", bytes(received))
            received.clear()
        link.deleteLater()
