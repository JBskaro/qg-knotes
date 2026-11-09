# SPDX-License-Identifier: GPL-3.0-only
"""The app log in %LOCALAPPDATA%\\QG Knotes.

The running copy of the program writes журнал.log and starts a new file past a megabyte. The processes started
alongside it (the MCP server for an AI assistant, transcription from the command line, the update swap) write files
of their own and never rename a file another process may hold: on Windows that rename fails and the log would stop.
Uncaught errors of the window thread and of other threads go to the log (the exe has no console), then to whoever
listens: the program shows them as a toast.
"""

import logging
import logging.handlers
import os
import pathlib
import sys
import threading
import time
from collections.abc import Callable

from knotes import settings

FILE = "журнал.log"
SIDE_FILES = {"mcp": "журнал_mcp.log", "transcribe": "журнал_распознавания.log", "swap": "журнал_обновления.log"}
LIMIT = 1_000_000
BACKUPS = 3
RETRY_S = 600  # a log held by another program is not rotated again for this long
FORMAT = "%(asctime)s %(process)d %(levelname)s %(name)s: %(message)s"
log = logging.getLogger(__name__)
_listener: dict[str, Callable[[BaseException], None]] = {}


class _Rotating(logging.handlers.RotatingFileHandler):
    """Rotation that waits instead of losing the log when an antivirus or an editor holds the file."""

    def __init__(self, path: pathlib.Path):
        super().__init__(path, maxBytes=LIMIT, backupCount=BACKUPS, encoding="utf-8")
        self._next_try = 0.0

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        return time.monotonic() >= self._next_try and bool(super().shouldRollover(record))

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError:
            self._next_try = time.monotonic() + RETRY_S
            if self.stream is None:
                self.stream = self._open()


def path(side: str = "") -> pathlib.Path:
    """The log of the running copy, or of a side process: "mcp", "transcribe", "swap"."""
    return settings.log_folder() / (SIDE_FILES[side] if side else FILE)


def _side_handler(file: pathlib.Path) -> logging.Handler:
    """A side process sets a big file aside when it starts; if another copy holds it, it just goes on writing."""
    try:
        if file.stat().st_size > LIMIT:
            os.replace(file, file.with_name(file.name + ".1"))
    except OSError:  # no file yet, or held open by another copy
        pass
    return logging.FileHandler(file, encoding="utf-8")


def setup(side: str = "") -> logging.Handler | None:
    """The log of this process; None when its folder cannot be created (then messages go to stderr)."""
    file = path(side)
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        handler = _side_handler(file) if side else _Rotating(file)
    except OSError:
        logging.basicConfig(level=logging.INFO, format=FORMAT)
        return None
    handler.setFormatter(logging.Formatter(FORMAT))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return handler


def listen(tell: Callable[[BaseException], None] | None) -> None:
    """tell(error) after an uncaught error is logged, from any thread; None: nobody listens any more."""
    _listener.clear()
    if tell is not None:
        _listener["кому"] = tell


def _tell(error: BaseException) -> None:
    tell = _listener.get("кому")
    if tell is None:
        return
    try:
        tell(error)
    except RuntimeError:  # the program is closing: nobody to show it to
        log.info("uncaught error after the program closed")


def _window_error(kind: type[BaseException], error: BaseException, trace: object) -> None:
    log.error("uncaught error", exc_info=(kind, error, trace))
    _tell(error)


def _thread_error(arguments: threading.ExceptHookArgs) -> None:
    if arguments.exc_type is SystemExit:
        return
    name = arguments.thread.name if arguments.thread else "?"
    log.error(
        "uncaught error in thread %s",
        name,
        exc_info=(arguments.exc_type, arguments.exc_value, arguments.exc_traceback),
    )
    if arguments.exc_value is not None:
        _tell(arguments.exc_value)


def catch_uncaught() -> None:
    """Errors nobody caught, in the window thread and in any other thread, go to the log and to the listeners."""
    sys.excepthook = _window_error
    threading.excepthook = _thread_error
