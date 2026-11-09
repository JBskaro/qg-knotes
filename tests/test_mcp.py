# SPDX-License-Identifier: GPL-3.0-only
"""MCP: handshake, tool list and a call over JSON-RPC line by line through stdin and stdout."""

import io
import json
import unittest

from knotes import mcp


def _stack(a: int, b: int = 0) -> dict:
    if a < 0:
        raise mcp.ToolError("Отрицательное число")
    return {"сумма": a + b}


TOOLS = {
    "add": mcp.Tool(
        "add", "Сложить", {"a": {"type": "integer"}, "b": {"type": "integer"}}, _stack, ("a",), read_only=True
    )
}


def _exchange(*messages: dict | str) -> list[dict]:
    input_ = io.StringIO(
        "".join((s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)) + "\n" for s in messages)
    )
    exit_ = io.StringIO()
    mcp.serve(TOOLS, "Подсказка", input_, exit_)
    return [json.loads(s) for s in exit_.getvalue().splitlines()]


class TestParamsCheck(unittest.TestCase):
    def test_type_length_and_enum_checked_before_action(self):
        """A task description as a dict instead of a string would be saved and break the board on every display."""
        recorded_amount: list = []
        tool = mcp.Tool(
            "t",
            "т",
            {
                "text": {"type": "string", "maxLength": 5},
                "labels": {"type": "array", "items": {"type": "string"}},
                "n": {"type": "integer"},
                "kind": {"type": "string", "enum": ["а"]},
            },
            lambda **p: recorded_amount.append(p) or {},
        )
        for arguments in (
            {"text": {"a": 1}},
            {"text": "длинный текст"},
            {"labels": ["ок", 3]},
            {"n": True},
            {"kind": "б"},
        ):
            with self.subTest(arguments=arguments):
                self.assertTrue(mcp._call(tool, arguments)["isError"])
        self.assertEqual(recorded_amount, [])
        self.assertFalse(mcp._call(tool, {"text": "ок", "labels": ["а"], "n": 2, "kind": "а"})["isError"])


class TestMinutes(unittest.TestCase):
    def test_handshake_and_list(self):
        answers = _exchange(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "т"}},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},  # a notification gets no answer
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        )
        self.assertEqual([o["id"] for o in answers], [1, 2, 3])
        start = answers[0]["result"]
        self.assertEqual(
            (start["protocolVersion"], start["serverInfo"]["name"], start["instructions"]),
            ("2025-03-26", "qg-knotes", "Подсказка"),
        )
        self.assertIn("tools", start["capabilities"])
        description = answers[1]["result"]["tools"][0]
        self.assertEqual(
            (description["name"], description["inputSchema"]["required"], description["annotations"]),
            ("add", ["a"], {"readOnlyHint": True}),
        )
        self.assertEqual(answers[2]["result"], {})

    def test_unknown_client_version(self):
        answer = _exchange(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}
        )[0]
        self.assertEqual(answer["result"]["protocolVersion"], mcp.VERSIONS[0])

    def test_call_result_and_failures(self):
        answers = _exchange(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "add", "arguments": {"a": 2, "b": 3}},
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "add", "arguments": {"a": -1}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "add", "arguments": {"c": 1}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "нет", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 5, "method": "resources/list"},
            "не json",
        )
        result = answers[0]["result"]
        self.assertEqual((json.loads(result["content"][0]["text"]), result["isError"]), ({"сумма": 5}, False))
        self.assertEqual(
            (answers[1]["result"]["content"][0]["text"], answers[1]["result"]["isError"]), ("Отрицательное число", True)
        )  # a clear refusal goes to the model, the server keeps running
        self.assertTrue(answers[2]["result"]["isError"])  # wrong parameters
        self.assertEqual(answers[3]["error"]["code"], -32602)
        self.assertEqual(answers[4]["error"]["code"], -32601)
        self.assertEqual(answers[5]["error"]["code"], -32700)

    def test_cyrillic_arrives_and_leaves_intact(self):
        exit_ = io.StringIO()
        mcp.serve(
            {"add": mcp.Tool("add", "С", {}, lambda: {"текст": "Решение принято"})},
            "",
            io.StringIO('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"add"}}\n'),
            exit_,
        )
        self.assertTrue(exit_.getvalue().isascii())  # only ASCII into the pipe: the pipe encoding does not matter
        text = json.loads(json.loads(exit_.getvalue())["result"]["content"][0]["text"])
        self.assertEqual(text, {"текст": "Решение принято"})


if __name__ == "__main__":
    unittest.main()
