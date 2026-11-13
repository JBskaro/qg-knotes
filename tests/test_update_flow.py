# SPDX-License-Identifier: GPL-3.0-only
"""Updates of the program: when the check runs, what the user is told, not during a recording or a recognition,
the installer or the swap of the portable copy."""

import contextlib
import dataclasses
import datetime
import pathlib
import unittest
from collections.abc import Callable
from unittest import mock

from knotes import VERSION, settings, update_flow, updates

FILE = updates.Asset("QG-Knotes-setup.exe", "https://example.org/setup.exe", 10, "00")


def release(version: str, notes: str = "") -> updates.Release:
    return updates.Release(version, notes, "https://example.org", (FILE,))


def at_once(_pool: object, work: Callable[[], object], on_result: Callable[[object], None]) -> None:
    """run_in_background without a thread: the result, or the exception, goes to on_result."""
    try:
        result = work()
    except Exception as error:  # noqa: BLE001 - as the background job does
        result = error
    on_result(result)


class TestUpdates(unittest.TestCase):
    def setUp(self):
        self.busy = False
        self.flow = update_flow.Updates(lambda: self.busy)
        self.notices: list[str] = []
        self.checked: list[str] = []
        self.offered: list[tuple] = []
        self.started: list[bool] = []
        self.flow.notice.connect(lambda heading, _text: self.notices.append(heading))
        self.flow.checked.connect(self.checked.append)
        self.flow.offered.connect(lambda release_, point: self.offered.append((release_.version, point)))
        self.flow.started.connect(lambda: self.started.append(True))
        for patch in (
            mock.patch.object(update_flow, "run_in_background", at_once),
            mock.patch.object(update_flow.updates, "frozen", return_value=True),
            mock.patch.object(update_flow.updates, "installed", return_value=True),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _latest(self, answer: object) -> mock._patch:
        return mock.patch.object(update_flow.updates, "latest", side_effect=[answer])

    def test_check_by_itself_once_a_day_in_the_build_only(self):
        today = datetime.date.today().isoformat()
        for config in (
            settings.Settings(check_for_updates=False),
            settings.Settings(updates_checked=today),
        ):
            with self.subTest(config=config), mock.patch.object(update_flow.updates, "latest") as latest:
                self.flow.check(config)
            latest.assert_not_called()
        with (
            mock.patch.object(update_flow.updates, "frozen", return_value=False),
            mock.patch.object(update_flow.updates, "latest") as latest,
        ):
            self.flow.check(settings.Settings())
        latest.assert_not_called()
        with self._latest(release("99.0.0", "\n - Заметки встречи\n- Другое")):
            self.flow.check(settings.Settings(updates_checked=today), manual=True)
        self.assertEqual(self.checked, [""])
        self.assertEqual(self.offered, [("99.0.0", "Заметки встречи")])

    def test_pending_release_is_checked_again_after_restart(self):
        config = settings.Settings()
        with mock.patch.object(update_flow.updates, "latest", return_value=release("99.0.0")) as latest:
            self.flow.check(config)
            self.flow.check(config)
            restarted = update_flow.Updates(lambda: False)
            restarted.check(dataclasses.replace(config, updates_checked=self.checked[-1]))
        self.assertEqual(latest.call_count, 2)
        self.assertEqual(self.offered, [("99.0.0", "")])

    def test_what_a_manual_check_tells(self):
        """Without a newer release, or without an answer, a check from the tray menu says so; by itself it is
        silent."""
        for answer, heading in (
            (release(VERSION), "Обновлений нет"),
            (None, "Обновления не проверить"),
            (OSError(5, "сеть"), "Обновления не проверить"),
        ):
            for manual in (False, True):
                self.notices.clear()
                with self.subTest(answer=answer, manual=manual), self._latest(answer), self.assertNoLogs(level="ERROR"):
                    self.flow.check(settings.Settings(), manual)
                self.assertEqual(self.notices, [heading] if manual else [])
        self.assertEqual(self.offered, [])
        self.assertEqual(len(self.checked), 3)  # the failed requests do not count as a check

    def test_install_not_during_work_and_only_in_the_build(self):
        self.busy = True
        with mock.patch.object(update_flow.updates, "download") as download:
            self.flow.install(release("99.0.0"))
        self.assertEqual(self.notices, ["Обновлю позже"])
        self.busy = False
        with mock.patch.object(update_flow.updates, "frozen", return_value=False):
            self.flow.install(release("99.0.0"))
        self.assertEqual(self.notices[-1], "Обновление не поставить")
        self.flow.install(dataclasses.replace(release("99.0.0"), assets=()))
        self.assertEqual(self.notices[-1], "Обновление не поставить")
        download.assert_not_called()

    def test_installer_runs_and_the_program_exits(self):
        with (
            mock.patch.object(update_flow.updates, "download", return_value=pathlib.Path("setup.exe")),
            mock.patch.object(update_flow.updates, "run_setup") as setup,
        ):
            self.flow.install(release("99.0.0"))
        setup.assert_called_once_with(pathlib.Path("setup.exe"))
        self.assertEqual(self.started, [True])

    def test_portable_copy_is_swapped(self):
        with (
            mock.patch.object(update_flow.updates, "installed", return_value=False),
            mock.patch.object(update_flow.updates, "asset_for", return_value=FILE),
            mock.patch.object(update_flow.updates, "download", return_value=pathlib.Path("копия.zip")),
            mock.patch.object(update_flow.updates, "stage_portable", return_value=pathlib.Path("новая")),
            mock.patch.object(update_flow.updates, "start_swap") as swap,
        ):
            self.flow.install(release("99.0.0"))
        swap.assert_called_once_with(pathlib.Path("новая"))
        self.assertEqual(self.started, [True])

    def test_failures_while_installing_are_told(self):
        """A failed download, a recording started meanwhile, an installer that does not start: the program stays."""
        cases = (
            {"download": OSError(28, "диск")},
            {"download": pathlib.Path("setup.exe"), "busy": True},
            {"download": pathlib.Path("setup.exe"), "run_setup": PermissionError(13, "нет доступа")},
        )
        for case in cases:
            self.notices.clear()
            with (
                self.subTest(case=case),
                mock.patch.object(update_flow.updates, "download", side_effect=[case["download"]]),
                mock.patch.object(update_flow.updates, "run_setup", side_effect=case.get("run_setup")),
                mock.patch.object(self.flow, "_busy", side_effect=[False, case.get("busy", False)]),
                self.assertLogs(update_flow.log, "WARNING") if "busy" not in case else contextlib.nullcontext(),
            ):
                self.flow.install(release("99.0.0"))
            self.assertEqual(len(self.notices), 2)  # "Downloading" and why it did not go on
        self.assertEqual(self.started, [])


if __name__ == "__main__":
    unittest.main()
