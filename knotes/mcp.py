# SPDX-License-Identifier: GPL-3.0-only
"""MCP: the program's tools for AI (Claude and other clients) over standard input and output, JSON-RPC line by line.

A short implementation of our own: only initialize, tools and prompts are needed, and the official library pulls
in a web server and 24 more packages. Compatibility is checked with the official MCP client. A tool refusal
(ToolError) goes to the model as text with isError, and the server keeps running.
"""

import dataclasses
import inspect
import json
import logging
from collections.abc import Callable
from typing import IO

from knotes import VERSION

VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # the first one if the client asks for an unknown one
log = logging.getLogger(__name__)


class ToolError(Exception):
    """A clear refusal for the model: what is wrong and what to do."""


@dataclasses.dataclass(frozen=True)
class Tool:
    name: str
    description: str
    params: dict  # JSON Schema properties; names in Latin letters, as the action's parameters
    action: Callable[..., dict]
    required: tuple[str, ...] = ()
    read_only: bool = False
    changes_data: bool = False  # replaces data (the minutes): the client should ask the user

    def describe(self) -> dict:
        tips = {"readOnlyHint": True} if self.read_only else {"destructiveHint": self.changes_data}
        return {
            "name": self.name,
            "description": self.description,
            "annotations": tips,
            "inputSchema": {"type": "object", "properties": self.params, "required": list(self.required)},
        }


@dataclasses.dataclass(frozen=True)
class Prompt:
    """A ready-made request the client offers in its menu (MCP prompt); all its arguments are required."""

    name: str
    description: str
    arguments: dict[str, str]  # name -> description
    text: Callable[..., str]

    def describe(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "arguments": [{"name": i, "description": o, "required": True} for i, o in self.arguments.items()],
        }


def _toast(id_: object, tips: dict[str, Prompt], params: dict) -> dict:
    name, arguments = params.get("name"), params.get("arguments") or {}
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return _error(id_, -32602, "name - строка, arguments - объект")
    tooltip = tips.get(name)
    if tooltip is None:
        return _error(id_, -32602, f"Нет подсказки {params.get('name')}")
    if none_ := [i for i in tooltip.arguments if not str(arguments.get(i, "")).strip()]:
        return _error(id_, -32602, f"Не хватает: {', '.join(none_)}")
    text = tooltip.text(**{i: str(arguments[i]) for i in tooltip.arguments})
    return _answer(
        id_,
        {"description": tooltip.description, "messages": [{"role": "user", "content": {"type": "text", "text": text}}]},
    )


def _answer(id_: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: object, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": text}}


def _text(text: str, error: bool) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


JSON_TYPES = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict}
LENGTH = 20000  # string parameter limit when the schema sets no maxLength of its own


def _check_value(name: str, value: object, scheme: dict) -> None:
    """The value has the schema's type, a string is within the limit, the value is from the enum if one is set."""
    kind = JSON_TYPES.get(scheme.get("type", ""))
    if kind is not None and (not isinstance(value, kind) or (kind is int and isinstance(value, bool))):
        raise ToolError(f"{name}: нужен {scheme['type']}")
    if isinstance(value, str) and len(value) > scheme.get("maxLength", LENGTH):
        raise ToolError(f"{name}: длиннее {scheme.get('maxLength', LENGTH)} знаков")
    if "enum" in scheme and value not in scheme["enum"]:
        raise ToolError(f"{name}: одно из {', '.join(map(str, scheme['enum']))}")
    nested = scheme.get("items") or scheme.get("additionalProperties")
    if isinstance(nested, dict):
        for part in value.values() if isinstance(value, dict) else value if isinstance(value, list) else ():
            _check_value(name, part, nested)


def _call(tool: Tool, arguments: dict) -> dict:
    try:
        for name, value in arguments.items():
            if name in tool.params and value is not None:
                _check_value(name, value, tool.params[name])
        try:
            inspect.signature(tool.action).bind(**arguments)
        except TypeError as error:  # an extra or missing parameter
            raise ToolError(f"Неверные параметры {tool.name}: {error}") from None
        return _text(json.dumps(tool.action(**arguments), ensure_ascii=False, indent=1), False)
    except ToolError as error:
        return _text(str(error), True)
    except Exception as error:  # noqa: BLE001 - top of the call: failure goes to the model and the log, server stays up
        log.exception("tool %s", tool.name)
        return _text(f"Сбой {tool.name}: {error}", True)


def reply(
    message: dict, tools: dict[str, Tool], instruction: str, tips: dict[str, Prompt] | None = None
) -> dict | None:
    """Reply to one client message; None for notifications and client replies."""
    method, id_, params = message.get("method"), message.get("id"), message.get("params") or {}
    tips = tips or {}
    if method is None or "id" not in message:
        return None
    if not isinstance(params, dict):
        return _error(id_, -32602, "params - объект")
    if method == "initialize":
        version = params.get("protocolVersion")
        capabilities = {"tools": {"listChanged": False}} | ({"prompts": {"listChanged": False}} if tips else {})
        return _answer(
            id_,
            {
                "protocolVersion": version if version in VERSIONS else VERSIONS[0],
                "capabilities": capabilities,
                "serverInfo": {"name": "qg-knotes", "version": VERSION},
                "instructions": instruction,
            },
        )
    if method == "prompts/list":
        return _answer(id_, {"prompts": [p.describe() for p in tips.values()]})
    if method == "prompts/get":
        return _toast(id_, tips, params)
    if method == "ping":
        return _answer(id_, {})
    if method == "tools/list":
        return _answer(id_, {"tools": [i.describe() for i in tools.values()]})
    if method == "tools/call":
        return _tool_call(id_, tools, params)
    return _error(id_, -32601, f"Метод {method} не поддерживается")


def _tool_call(id_: object, tools: dict[str, Tool], params: dict) -> dict:
    name, arguments = params.get("name"), params.get("arguments") or {}
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return _error(id_, -32602, "name - строка, arguments - объект")
    if (tool := tools.get(name)) is None:
        return _error(id_, -32602, f"Нет инструмента {name}")
    return _answer(id_, _call(tool, arguments))


def serve(
    tools: dict[str, Tool], instruction: str, input_: IO[str], exit_: IO[str], tips: dict[str, Prompt] | None = None
) -> None:
    """Read messages line by line until the input ends. Output is ASCII only (Cyrillic is escaped): the encoding of
    the channel between the programs does not matter."""
    for line in input_:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            answer: dict | None = _error(None, -32700, "Сообщение не JSON")
        else:
            answer = _reply_safely(message, tools, instruction, tips)
        if answer is not None:
            exit_.write(json.dumps(answer) + "\n")
            exit_.flush()


def _reply_safely(
    message: object, tools: dict[str, Tool], instruction: str, tips: dict[str, Prompt] | None
) -> dict | None:
    """A failure while replying to one message is logged and answered: the server keeps reading the next ones."""
    if not isinstance(message, dict):
        return _error(None, -32600, "Ожидался объект JSON-RPC")
    try:
        return reply(message, tools, instruction, tips)
    except Exception:  # noqa: BLE001 - top of the message loop: the server stays up for the next messages
        log.exception("MCP message %s", message.get("method"))
        return _error(message.get("id"), -32603, "Внутренняя ошибка сервера, подробности в журнале программы")
