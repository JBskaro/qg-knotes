# SPDX-License-Identifier: GPL-3.0-only
"""Entry point: each start loads only what it runs; side processes write logs of their own."""

import pathlib
import subprocess
import sys
import unittest
from unittest import mock

from knotes import entry, local_engine, updates

ROOT = pathlib.Path(__file__).parents[1]
HEAVY = ("knotes.app", "knotes.recording", "knotes.tasks", "knotes.ui.meetings_window")
LOADED = f"import sys; print(sorted(m for m in {HEAVY!r} if m in sys.modules))"


def _loaded(script: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", script + "\n" + LOADED],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=120,
    )
    return result.stdout.strip().splitlines()[-1]


class TestEntry(unittest.TestCase):
    def test_flags_are_those_of_the_modules(self):
        self.assertEqual(entry.ENGINE, local_engine.FLAG)
        self.assertEqual(entry.SWAP, updates.SWAP_FLAG)

    def test_engine_and_second_copy_do_not_load_the_program(self):
        """The local engine starts for every transcription, a second copy for every click on the shortcut: neither
        loads the windows, recording and recognition of the program."""
        engine = "from unittest import mock\nfrom knotes import entry\n"
        engine += "with mock.patch('knotes.local_engine.main', return_value=0):\n"
        engine += "    assert entry.main(['knotes', '--local-engine']) == 0"
        self.assertEqual(_loaded(engine), "[]")
        second = "from unittest import mock\nfrom knotes import entry\n"
        second += "with mock.patch('knotes.instance.send', return_value=True):\n"
        second += "    assert entry.main(['knotes']) == 0"
        self.assertEqual(_loaded(second), "[]")

    def test_side_processes_get_their_own_log(self):
        with (
            mock.patch("knotes.journal.setup") as journal,
            mock.patch("knotes.ai_tools.main", return_value=0) as mcp,
        ):
            self.assertEqual(entry.main(["knotes", "--mcp"]), 0)
        journal.assert_called_once_with("mcp")
        mcp.assert_called_once()
        with (
            mock.patch("knotes.journal.setup") as journal,
            mock.patch("knotes.recognition.main", return_value=0) as transcribe,
        ):
            self.assertEqual(entry.main(["knotes", "--transcribe", "запись.mp4"]), 0)
        journal.assert_called_once_with("transcribe")
        self.assertEqual(transcribe.call_args.args, (["запись.mp4"], entry.payment))
        with mock.patch("knotes.journal.setup") as journal, mock.patch("knotes.updates.finish_swap", return_value=0):
            self.assertEqual(entry.main(["knotes", "--update-swap", "новая", "старая", "1"]), 0)
        journal.assert_called_once_with("swap")

    def test_engine_and_copies(self):
        with mock.patch("knotes.local_engine.main", return_value=0) as engine:
            self.assertEqual(entry.main(["knotes", "--локально"]), 0)  # a flag of version 0.20
        engine.assert_called_once()
        with (
            mock.patch("PySide6.QtWidgets.QApplication") as application,
            mock.patch("knotes.instance.send", return_value=False),
            mock.patch("knotes.instance.first", side_effect=[None, "замок"]) as first,
            mock.patch("knotes.app.run", return_value=7) as run,
        ):
            self.assertEqual(entry.main(["knotes", "--quit"]), 0)  # nothing runs: nothing to quit
            self.assertEqual(entry.main(["knotes", "--record"]), 0)  # another copy is starting and got it
            self.assertEqual(entry.main(["knotes", "--restart", "--settings"]), 7)
        self.assertEqual(first.call_args.args, (b"settings", 20_000))
        run.assert_called_once_with(application.return_value, "замок", ["knotes", "--restart", "--settings"])

    def test_paid_parts_go_to_the_spending_log(self):
        with mock.patch("knotes.spending.write") as written:
            entry.payment(pathlib.Path("встреча"))("scribe", 2.5)
        written.assert_called_once_with(pathlib.Path("встреча"), "Речь", "scribe", 2.5)


if __name__ == "__main__":
    unittest.main()
