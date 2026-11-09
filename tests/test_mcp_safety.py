# SPDX-License-Identifier: GPL-3.0-only
"""MCP server and tools: a malformed message does not stop the server, a failure inside a tool is logged, a long
transcript comes in pieces that fit the client's answer limit, reading tools write nothing."""

import inspect
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from knotes import ai_tools, catalog, mcp, minutes, notes, settings, transcript


def _exchange(tools: dict, *messages: dict) -> list[dict]:
    input_ = io.StringIO("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in messages))
    exit_ = io.StringIO()
    mcp.serve(tools, "", input_, exit_)
    return [json.loads(s) for s in exit_.getvalue().splitlines()]


def _add(a: int, b: int = 0) -> dict:
    return {"сумма": a + b}


TOOLS = {"add": mcp.Tool("add", "Сложить", {"a": {"type": "integer"}, "b": {"type": "integer"}}, _add, ("a",))}
PING = {"jsonrpc": "2.0", "id": 99, "method": "ping"}


class TestServerSurvives(unittest.TestCase):
    def test_malformed_params_get_error_and_server_goes_on(self):
        answers = _exchange(
            TOOLS,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["add"]},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": ["add"]}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "add", "arguments": [1]}},
            {"jsonrpc": "2.0", "id": 4, "method": "prompts/get", "params": {"name": {"а": 1}}},
            {"jsonrpc": "2.0", "id": 5, "method": "initialize", "params": "2025-06-18"},
            PING,
        )
        self.assertEqual([o["id"] for o in answers], [1, 2, 3, 4, 5, 99])
        self.assertEqual([o["error"]["code"] for o in answers[:5]], [-32602] * 5)
        self.assertEqual(answers[-1]["result"], {})

    def test_failure_inside_reply_is_logged_and_answered(self):
        with (
            mock.patch.object(mcp, "reply", side_effect=[RuntimeError("сбой"), {"jsonrpc": "2.0", "id": 99}]),
            self.assertLogs("knotes.mcp", "ERROR"),
        ):
            answers = _exchange(TOOLS, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, PING)
        self.assertEqual((answers[0]["id"], answers[0]["error"]["code"]), (1, -32603))
        self.assertEqual(answers[1]["id"], 99)

    def test_wrong_arguments_refused_before_the_call(self):
        action = mock.Mock(return_value={})
        tool = mcp.Tool("t", "т", {"a": {"type": "integer"}}, action, ("a",))
        action.__signature__ = inspect.signature(_add)
        for arguments in ({}, {"a": 1, "c": 2}):
            with self.subTest(arguments=arguments):
                answer = mcp._call(tool, arguments)
                self.assertTrue(answer["isError"])
                self.assertIn("Неверные параметры t", answer["content"][0]["text"])
        action.assert_not_called()

    def test_type_error_inside_tool_is_a_logged_failure(self):
        def broken(a: int) -> dict:
            return {"x": a + "строка"}

        tool = mcp.Tool("t", "т", {"a": {"type": "integer"}}, broken, ("a",))
        with self.assertLogs("knotes.mcp", "ERROR"):
            answer = mcp._call(tool, {"a": 1})
        self.assertTrue(answer["isError"])
        self.assertIn("Сбой t", answer["content"][0]["text"])


class TestMeetingTools(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.recordings = pathlib.Path(self._tmp.name)
        patcher = mock.patch.object(
            settings, "load", return_value=settings.Settings(recordings_folder=str(self.recordings))
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = self.recordings / "2026-10-20_10-00"
        self.folder.mkdir()

    def _long_meeting(self, count: int) -> None:
        phrases = [
            {"начало": n * 10.0, "конец": n * 10.0 + 9, "кто": "Я", "текст": f"Фраза {n}. " + " ".join(["слово"] * 40)}
            for n in range(count)
        ]
        transcript.save(self.folder, phrases, {"длительность": count * 10, "провайдер": "т", "стоимость": 0}, [])

    def test_long_transcript_comes_in_pieces_without_repeats(self):
        self._long_meeting(600)  # about three hours of speech
        pieces, from_time = [], None
        while True:
            piece = ai_tools.get_transcript(self.folder.name, from_time=from_time)
            self.assertLessEqual(len(piece["текст"]), ai_tools.PIECE)
            pieces.append(piece["текст"])
            from_time = piece["следующий_from_time"]
            if from_time is None:
                break
        self.assertGreater(len(pieces), 3)
        lines = "\n".join(pieces).splitlines()
        self.assertEqual(len(lines), 600)
        self.assertEqual(
            lines, [f"[{transcript.time(n * 10)}] Я: Фраза {n}. " + " ".join(["слово"] * 40) for n in range(600)]
        )

    def test_short_transcript_whole(self):
        self._long_meeting(3)
        piece = ai_tools.get_transcript(self.folder.name)
        self.assertEqual((piece["строк"], piece["следующий_from_time"]), (3, None))

    def test_protocol_task_without_transcript_and_kind_not_saved(self):
        self._long_meeting(600)
        catalog.set_(self.folder, title="Обследование склада")
        before = sorted((p.name, p.stat().st_mtime_ns) for p in self.folder.iterdir())
        job = ai_tools.get_protocol_task(self.folder.name, kind="Обследование")
        self.assertEqual(sorted((p.name, p.stat().st_mtime_ns) for p in self.folder.iterdir()), before)
        self.assertNotIn("Фраза 1.", job["input"])
        self.assertIn("Вид встречи - Обследование", job["input"])
        self.assertIn("get_transcript", job["transcript"])
        self.assertLess(len(json.dumps(job, ensure_ascii=False)), ai_tools.PIECE)
        self.assertNotIn("вид", catalog.properties(self.folder))

    def test_meeting_notes_read_old_format_without_writing(self):
        self._long_meeting(3)
        transcript.mark(self.folder, 7.0, f"{transcript.NOTE} цена из прайса")
        before = sorted((p.name, p.stat().st_mtime_ns) for p in self.folder.iterdir())
        result = ai_tools.get_meeting_notes(self.folder.name)
        self.assertEqual(result["строки"], [{"время": "00:07", "текст": "цена из прайса"}])
        self.assertEqual(sorted((p.name, p.stat().st_mtime_ns) for p in self.folder.iterdir()), before)
        notes.move_old(self.folder)  # the explicit step: the notes go to заметки.md, the mark leaves the transcript
        self.assertTrue((self.folder / notes.FILE).exists())
        self.assertEqual(transcript.labels(self.folder), [])

    def test_notes_written_from_old_format_leave_marks(self):
        self._long_meeting(3)
        transcript.mark(self.folder, 7.0, f"{transcript.NOTE} цена из прайса")
        notepad = notes.read(self.folder)
        notes.write(self.folder, notes.Notes(notepad.agenda, (*notepad.lines, notes.NoteLine("еще"))))
        self.assertEqual([s.text for s in notes.read(self.folder).lines], ["цена из прайса", "еще"])
        self.assertEqual(transcript.labels(self.folder), [])
        self.assertNotIn(transcript.NOTE, (self.folder / transcript.TRANSCRIPT).read_text(encoding="utf-8"))

    def test_compose_moves_old_notes_first(self):
        self._long_meeting(3)
        transcript.mark(self.folder, 7.0, f"{transcript.NOTE} цена из прайса")
        params = mock.Mock(service=mock.Mock(key_needed=False, address="http://localhost:1/v1", kind="чат"))
        params.model, params.key = "м", ""
        with (
            mock.patch.object(minutes, "input_", return_value="вход") as input_,
            mock.patch.object(minutes.providers, "model_answer", return_value=("# Итоги\n", 0.0)),
        ):
            minutes.compose(self.folder, params)
        input_.assert_called_once()
        self.assertTrue((self.folder / notes.FILE).exists())


if __name__ == "__main__":
    unittest.main()
