# SPDX-License-Identifier: GPL-3.0-only
"""Entry point: the command line is read before anything heavy is loaded.

The local speech recognition engine starts for every transcription on this computer, the MCP server for every AI
assistant, a second copy for every click on the shortcut: each loads only what it runs, not the windows, recording and
recognition of the program itself.
"""

import ctypes
import pathlib
import sys
from collections.abc import Callable

from knotes import system

ENGINE = "--local-engine"  # local_engine.FLAG
SWAP = "--update-swap"  # updates.SWAP_FLAG
APP_ID = "JBskaro.QGKnotes"


def payment(folder: pathlib.Path) -> Callable[[str, float], None]:
    """Where speech recognition writes the paid parts of the meeting: to the spending log."""
    from knotes import catalog, spending  # noqa: PLC0415 - loaded by the processes that recognize speech

    return lambda model, how_many: spending.write(folder, catalog.SPEECH, model, how_many)


def main(arguments: list[str] | None = None) -> int:
    arguments = system.english_flags(sys.argv if arguments is None else arguments)
    if ENGINE in arguments:  # no windows: the local speech recognition engine; its log goes to stderr
        from knotes import local_engine  # noqa: PLC0415 - each start loads only what it runs

        return local_engine.main(arguments)
    if SWAP in arguments:  # no windows: the new copy replaces the previous one
        from knotes import journal, updates  # noqa: PLC0415

        journal.setup("swap")
        start = arguments.index(SWAP) + 1
        return updates.finish_swap(arguments[start : start + 3])
    if "--transcribe" in arguments:  # no windows: for scripts and Claude; result: exit code and расшифровка.md
        from knotes import journal, recognition  # noqa: PLC0415

        journal.setup("transcribe")
        return recognition.main(arguments[arguments.index("--transcribe") + 1 :], payment)
    if "--mcp" in arguments:  # no windows: the MCP server for Claude and other clients over stdin and stdout
        from knotes import ai_tools, journal  # noqa: PLC0415

        journal.setup("mcp")
        return ai_tools.main()
    return _windows(arguments)


def _windows(arguments: list[str]) -> int:
    """A second start passes its command to the running copy and exits without loading the program."""
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415

    from knotes import instance  # noqa: PLC0415

    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    application = QApplication(arguments)
    code = instance.command(arguments)
    restart = "--restart" in arguments  # the old copy is still exiting: wait for it, not send it a command
    if not restart and (instance.send(code) or code == instance.QUIT):
        return 0
    lock = instance.first(code, instance.WAIT_EXIT_MS if restart else 0)
    if lock is None:
        return 0
    from knotes import app  # noqa: PLC0415 - the first copy only

    return app.run(application, lock, arguments)
