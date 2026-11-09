# SPDX-License-Identifier: GPL-3.0-only
"""The window and the MCP server edit the same files at the same time: neither loses the other's edit.

The first edit runs in a thread and stops right before it writes; the second one, in another thread or in a separate
process like the MCP server, gets its turn meanwhile. Without a lock it writes at once and the first edit, going on
from what it read before, writes over it."""

import contextlib
import datetime
import errno
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable, Iterator
from unittest import mock

from knotes import ai_tools, catalog, failures, minutes, notes, privacy, projects, settings, spending, tasks
from knotes import transcript
from knotes.mcp import ToolError

ROOT = pathlib.Path(__file__).resolve().parents[1]
WAIT = 10  # seconds a step may take at most: a hang fails the test instead of blocking the run
GIVEN = 0.5  # seconds the second edit gets while the first one is stopped; without a lock it is done by then
SET_PROJECT = (
    "import pathlib, sys\n"
    "from knotes import catalog\n"
    "print('ready', flush=True)\n"
    "catalog.set_(pathlib.Path(sys.argv[1]), project=sys.argv[2])\n"
)
ADD_EXPENSE = (
    "import datetime, pathlib, sys\n"
    "from knotes import catalog, spending\n"
    "print('ready', flush=True)\n"
    "spending.add(pathlib.Path(sys.argv[1]), spending.Expense(datetime.datetime(2026, 10, 22, 12), sys.argv[2], "
    "catalog.MINUTES_KIND, 'b', 0.03))\n"
)
HOLD = (
    "import pathlib, sys, time\n"
    "from knotes import transcript\n"
    "with transcript.locked(pathlib.Path(sys.argv[1])):\n"
    "    print('ready', flush=True)\n"
    "    time.sleep(60)\n"
)


class _Edit(threading.Thread):
    """An edit in its own thread, standing for the window or the MCP server; its failure is kept for the test."""

    def __init__(self, action: Callable[[], object]) -> None:
        super().__init__(daemon=True)
        self.action, self.error = action, None

    def run(self) -> None:
        try:
            self.action()
        except BaseException as error:  # noqa: BLE001 - the test raises it in its own thread
            self.error = error

    def finish(self, test: unittest.TestCase) -> None:
        self.join(WAIT)
        test.assertFalse(self.is_alive(), "the edit hung")
        if self.error is not None:
            raise self.error


@contextlib.contextmanager
def _stopped(
    test: unittest.TestCase, action: Callable[[], object], pause: tuple = (transcript, "write_text")
) -> Iterator:
    """The edit runs in a thread and stops at its first call of pause until the block ends, then goes on."""
    stopped, go = threading.Event(), threading.Event()
    real = getattr(*pause)
    editor = _Edit(action)

    def stopping(*arguments: object, **named: object) -> object:
        if threading.current_thread() is editor and not go.is_set():
            stopped.set()
            go.wait(WAIT)
        return real(*arguments, **named)

    with mock.patch.object(*pause, stopping):
        editor.start()
        test.assertTrue(stopped.wait(WAIT), "the edit did not get to its write")
        try:
            yield
        finally:
            go.set()
            editor.join(WAIT)
    editor.finish(test)


def _meanwhile(action: Callable[[], object]) -> _Edit:
    """The second edit, given its time while the first one is stopped."""
    edit = _Edit(action)
    edit.start()
    edit.join(GIVEN)
    return edit


def _process(test: unittest.TestCase, code: str, *arguments: object) -> subprocess.Popen:
    """The second edit in a separate process, as the MCP server makes it; given its time once it has started."""
    child = subprocess.Popen(
        [sys.executable, "-c", code, *map(str, arguments)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    test.addCleanup(_ended, child)
    if child.stdout.readline().strip() != "ready":
        test.fail(f"the process did not start: {child.communicate(timeout=WAIT)[1]}")
    with contextlib.suppress(subprocess.TimeoutExpired):
        child.wait(GIVEN)
    return child


def _ended(child: subprocess.Popen) -> None:
    """A process a test left running is stopped, its pipes are closed."""
    child.kill()
    child.communicate()


def _finished(test: unittest.TestCase, child: subprocess.Popen) -> None:
    _, errors = child.communicate(timeout=WAIT)
    test.assertEqual(child.returncode, 0, errors)


class _Meeting(unittest.TestCase):
    """A recognized meeting with two other participants in the recordings folder."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)  # after the processes of the test are stopped: Windows keeps a held file
        self.recordings = pathlib.Path(tmp.name)
        self.folder = self.recordings / "2026-10-22_10-00"
        self.folder.mkdir()
        phrases = [
            {"начало": 1.0, "конец": 9.0, "кто": "Я", "текст": "Цену берем из прайса."},
            {"начало": 10.0, "конец": 19.0, "кто": "Собеседник 1", "текст": "Да, из прайса."},
            {"начало": 20.0, "конец": 29.0, "кто": "Собеседник 2", "текст": "Коробки считаем поштучно."},
        ]
        info = {"длительность": 60, "провайдер": "OpenRouter, модель-речи", "стоимость": 0.1}
        transcript.save(self.folder, phrases, info, [])
        patcher = mock.patch.object(
            settings, "load", return_value=settings.Settings(recordings_folder=str(self.recordings))
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class TestEditsAtOnce(_Meeting):
    def test_meeting_properties_from_window_and_mcp_process(self):
        with _stopped(self, lambda: catalog.set_(self.folder, title="Созвон по складу")):
            child = _process(self, SET_PROJECT, self.folder, "Склад")
        _finished(self, child)
        self.assertEqual(catalog.properties(self.folder), {"название": "Созвон по складу", "проект": "Склад"})

    def test_spending_log_started_by_window_and_mcp_process(self):
        """The first expense starts the log with the meeting prices: once, not by each process."""
        expense = spending.Expense(
            datetime.datetime(2026, 10, 22, 11), self.folder.name, catalog.MINUTES_KIND, "a", 0.02
        )
        with _stopped(self, lambda: spending.add(self.recordings, expense), pause=(spending, "from_meetings")):
            child = _process(self, ADD_EXPENSE, self.recordings, self.folder.name)
        _finished(self, child)
        logged = spending.list_(self.recordings, catalog.list_(self.recordings))
        self.assertEqual(
            sorted((e.kind, e.model, e.how_many) for e in logged),
            [
                (catalog.MINUTES_KIND, "a", 0.02),
                (catalog.MINUTES_KIND, "b", 0.03),
                (catalog.SPEECH, "модель-речи", 0.1),
            ],
        )

    def test_speaker_names_from_window_and_assistant(self):
        def window() -> None:  # as the meeting window renames a participant: the names it has read, with the new one
            transcript.set_names(self.folder, transcript.names(self.folder) | {"Собеседник 1": "Анна"})

        with _stopped(self, window):
            assistant = _meanwhile(lambda: ai_tools.set_speaker_names(self.folder.name, {"Собеседник 2": "Борис"}))
        assistant.finish(self)
        self.assertEqual(transcript.names(self.folder), {"Собеседник 1": "Анна", "Собеседник 2": "Борис"})
        text = (self.folder / transcript.TRANSCRIPT).read_text(encoding="utf-8")
        self.assertIn("Анна: Да, из прайса.", text)
        self.assertIn("Борис: Коробки считаем поштучно.", text)

    def test_mark_added_while_another_is_deleted(self):
        transcript.mark(self.folder, 5.0)
        with _stopped(self, lambda: transcript.mark(self.folder, 30.0)):
            deleting = _meanwhile(lambda: transcript.delete_label(self.folder, 5.0, transcript.MARKER))
        deleting.finish(self)
        self.assertEqual([m["время"] for m in transcript.labels(self.folder)], [30.0])
        self.assertNotIn("[00:05]", (self.folder / transcript.TRANSCRIPT).read_text(encoding="utf-8"))

    def test_note_lines_added_at_once(self):
        with _stopped(self, lambda: notes.add(self.folder, 10, "Цена из прайса")):
            second = _meanwhile(lambda: notes.add(self.folder, 20, "Коробки поштучно"))
        second.finish(self)
        self.assertEqual(
            [(s.second, s.text) for s in notes.read(self.folder).lines],
            [(10, "Цена из прайса"), (20, "Коробки поштучно")],
        )

    def test_personal_data_labels_from_two_assistant_calls(self):
        phone, email = privacy.Hider(table=privacy.table(self.folder)), privacy.Hider(table=privacy.table(self.folder))
        hidden_phone, hidden_email = phone.hide("Звоните +7 912 345-67-89"), email.hide("Пишите anna@example.ru")
        with _stopped(self, lambda: privacy.remember(self.folder, phone)):
            second = _meanwhile(lambda: privacy.remember(self.folder, email))
        second.finish(self)
        self.assertEqual(privacy.restored(self.folder, hidden_phone), "Звоните +7 912 345-67-89")
        self.assertEqual(privacy.restored(self.folder, hidden_email), "Пишите anna@example.ru")

    def test_minutes_composed_in_window_during_assistant_edit(self):
        """The window's new minutes are either the current ones or kept as the previous ones, never gone."""
        (self.folder / minutes.FILE).write_text("# Черновик\n", encoding="utf-8")
        params = mock.Mock(service=mock.Mock(key_needed=False, address="http://localhost:1/v1", kind="чат"))
        params.model, params.key = "м", ""
        with (
            mock.patch.object(minutes.providers, "model_answer", return_value=("# Протокол окна\n", 0.0)),
            mock.patch.object(minutes, "input_", return_value="вход"),
            _stopped(self, lambda: ai_tools.update_protocol(self.folder.name, "# Правка помощника")),
        ):
            window = _meanwhile(lambda: minutes.compose(self.folder, params))
        window.finish(self)
        texts = {p.name: p.read_text(encoding="utf-8") for p in self.folder.glob("протокол*.md")}
        self.assertIn("# Протокол окна\n", texts.values(), texts)
        self.assertIn("# Черновик\n", texts.values(), texts)

    def test_meeting_kind_chosen_in_window_during_failed_compose(self):
        """Minutes take a minute or two: when they fail, the tool puts back only the kind it set itself."""
        for kind, chosen in (("Обследование", "Показ доработки"), (None, "Статус проекта")):

            def window_chooses_then_fails(*_arguments: object, chosen: str = chosen, **_named: object) -> None:
                catalog.set_(self.folder, kind=chosen)
                raise OSError("нет сети")

            with (
                mock.patch.object(minutes, "compose", side_effect=window_chooses_then_fails),
                mock.patch.object(minutes, "params"),
                self.subTest(kind=kind),
                self.assertRaises(ToolError),
            ):
                ai_tools.compose_protocol(self.folder.name, kind=kind)
            self.assertEqual(catalog.properties(self.folder)["вид"], chosen)

    def test_settings_saved_while_a_broken_field_is_repaired(self):
        path = self.recordings / "настройки.json"
        path.write_text('{"язык": "ru", "тема": 5}', encoding="utf-8")
        with self.assertLogs(level="WARNING"), _stopped(self, lambda: settings.read(path)):
            saving = _meanwhile(lambda: settings.save(settings.Settings(language="en"), path))
        saving.finish(self)
        self.assertEqual(settings.read(path, set_aside=False)[0].language, "en")


class TestLock(_Meeting):
    def _held(self) -> tuple[_Edit, threading.Event]:
        """Another thread holds the lock of the meeting folder until the event is set."""
        holding, release = threading.Event(), threading.Event()

        def hold() -> None:
            with transcript.locked(self.folder / catalog.PROPERTIES):
                holding.set()
                release.wait(WAIT)

        holder = _Edit(hold)
        holder.start()
        self.assertTrue(holding.wait(WAIT))
        self.addCleanup(release.set)
        return holder, release

    def test_edit_waits_then_gives_up_with_clear_text_and_writes_nothing(self):
        catalog.set_(self.folder, title="Созвон")
        holder, release = self._held()
        with (
            mock.patch.object(transcript, "LOCK_WAIT_S", 0.2),
            self.assertLogs(level="WARNING"),
            self.assertRaises(transcript.Busy) as raised,
        ):
            catalog.set_(self.folder, project="Склад")
        release.set()
        holder.finish(self)
        self.assertIsInstance(raised.exception, OSError)  # the places that report disk errors report it too
        self.assertEqual(
            failures.text(raised.exception),
            f"Файлы в папке {self.folder} сейчас меняет QG Knotes или ваш помощник ИИ. Ничего не потеряно: повторите "
            "через несколько секунд.",
        )
        self.assertEqual(catalog.properties(self.folder), {"название": "Созвон"})
        catalog.set_(self.folder, project="Склад")
        self.assertEqual(catalog.properties(self.folder), {"название": "Созвон", "проект": "Склад"})

    def test_lock_of_a_process_that_died_mid_edit_does_not_stay(self):
        child = _process(self, HOLD, self.folder / catalog.PROPERTIES)
        with (
            mock.patch.object(transcript, "LOCK_WAIT_S", 0.2),
            self.assertLogs(level="WARNING"),
            self.assertRaises(transcript.Busy),
        ):
            catalog.set_(self.folder, title="Пока держит")
        child.kill()
        child.wait(WAIT)
        catalog.set_(self.folder, title="После сбоя")
        self.assertEqual(catalog.properties(self.folder), {"название": "После сбоя"})
        self.assertTrue((self.folder / transcript.LOCK).exists())  # the file stays, its lock does not

    def test_reading_does_not_wait(self):
        notes.add(self.folder, 10, "Цена из прайса")
        self._held()
        with mock.patch.object(transcript, "LOCK_WAIT_S", 0.1):  # a read that waited would fail with Busy
            self.assertEqual(catalog.properties(self.folder), {})
            self.assertEqual(transcript.names(self.folder), {})
            self.assertEqual(notes.with_minutes(self.folder), [(10, "Цена из прайса")])
            self.assertEqual(ai_tools.get_transcript(self.folder.name)["строк"], 3)
            self.assertEqual(ai_tools.get_meeting_notes(self.folder.name)["строки"][0]["текст"], "Цена из прайса")
            self.assertEqual(len(catalog.list_(self.recordings)), 1)

    def test_edit_inside_an_edit_of_the_same_folder_does_not_wait_for_itself(self):
        with mock.patch.object(transcript, "LOCK_WAIT_S", 0.1), transcript.locked(self.folder / notes.FILE):
            notes.add(self.folder, 10, "Цена из прайса")
            catalog.set_(self.folder, title="Созвон")
        self.assertEqual(catalog.properties(self.folder), {"название": "Созвон"})

    def test_failed_change_writes_nothing_and_releases_the_lock(self):
        def broken(_data: object) -> object:
            raise ValueError("правка не удалась")

        with self.assertRaises(ValueError):
            transcript.update_json(self.folder / catalog.PROPERTIES, {}, broken)
        self.assertFalse((self.folder / catalog.PROPERTIES).exists())
        with mock.patch.object(transcript, "LOCK_WAIT_S", 0.1):
            catalog.set_(self.folder, title="Созвон")
        self.assertEqual(catalog.properties(self.folder), {"название": "Созвон"})

    def test_unchanged_data_is_not_written_again(self):
        catalog.set_(self.folder, title="Созвон")
        with mock.patch.object(transcript, "write_text") as write:
            catalog.set_(self.folder, title="Созвон")
            transcript.delete_label(self.folder, 5.0, transcript.MARKER)  # no such mark
        write.assert_called_once()  # only the transcript is rebuilt
        self.assertEqual(write.call_args.args[0].name, transcript.TRANSCRIPT)

    def test_file_system_without_locks_edits_as_before(self):
        with (
            mock.patch.object(transcript, "_try_lock", side_effect=OSError(errno.EINVAL, "not supported")),
            self.assertLogs(level="WARNING") as log,
        ):
            catalog.set_(self.folder, title="Созвон")
        self.assertIn("without the lock", "\n".join(log.output))
        self.assertEqual(catalog.properties(self.folder), {"название": "Созвон"})

    def test_folder_that_is_gone_is_not_made_again(self):
        """A meeting deleted while its window is open: the edit fails as before, the folder does not come back."""
        gone = self.recordings / "2026-10-21_09-00"
        with self.assertRaises(FileNotFoundError):
            catalog.set_(gone, title="Созвон")
        self.assertFalse(gone.exists())


class TestBoardAndProjects(unittest.TestCase):
    """The board and the projects of the recordings folder (.узелок) are edited from the window and by the AI
    assistant: one lock for both files, held from reading to writing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.recordings = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_two_board_edits_keep_both(self):
        with _stopped(self, lambda: tasks.add(self.recordings, "Первая", "Склад")):
            second = _meanwhile(lambda: tasks.add(self.recordings, "Вторая", "Склад"))
        second.finish(self)
        self.assertEqual(sorted(z.text for z in tasks.tasks(self.recordings, "Склад", [])), ["Вторая", "Первая"])

    def test_column_and_project_details_keep_both(self):
        with _stopped(self, lambda: projects.add_column(self.recordings, "Склад", "Ждет ответа", "сделать")):
            second = _meanwhile(lambda: projects.set_kind(self.recordings, "Склад", "Обследование"))
        second.finish(self)
        self.assertIn("Ждет ответа", [c.name for c in projects.columns(self.recordings, "Склад")])
        self.assertEqual(projects.kind(self.recordings, "Склад"), "Обследование")


if __name__ == "__main__":
    unittest.main()
