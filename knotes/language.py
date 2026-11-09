# SPDX-License-Identifier: GPL-3.0-only
"""Interface language: Russian or English.

Strings shown to the user go through tr(): the text in code is the source, translations/<language>/*.json give it in
that language, and a string without a translation is shown as written; a test checks that nothing is missing. Most
strings in code are English with a Russian translation. Constants marked with tr_key() and word forms for count()
are Russian with an English translation: some of them are data too (stored values, text for the model). Placeholders
are named: tr("Meetings: {n}", n=3). Module constants are marked with tr_key("...") and translated when shown,
tr(CONSTANT), because the language is set at start, after the modules are loaded. An empty setting means the
language of Windows.
"""

import ctypes
import functools
import json
import pathlib

LANGUAGES = {"ru": "Русский", "en": "English"}
FOLDER = pathlib.Path(__file__).with_name("translations")
RUSSIAN_WINDOWS = 0x19  # primary language in the Windows UI language code
_CURRENT = {"код": "ru"}


def windows_language() -> str:
    try:
        code = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    except (AttributeError, OSError):
        return "ru"
    return "ru" if code & 0x3FF == RUSSIAN_WINDOWS else "en"


def which(code: str) -> str:
    """Language by the code from the settings: an empty or unknown code - the Windows language."""
    return code if code in LANGUAGES else windows_language()


def set_(code: str) -> None:
    _CURRENT["код"] = which(code)


def current() -> str:
    return _CURRENT["код"]


@functools.cache
def vocabulary(code: str) -> dict[str, str]:
    """All translation files of the language as one dictionary."""
    result: dict[str, str] = {}
    for file in sorted((FOLDER / code).glob("*.json")):
        result |= json.loads(file.read_text(encoding="utf-8"))
    return result


def tr_key(text: str) -> str:
    """Marks a constant string for translation; tr() translates it when shown."""
    return text


def forms(one: str, two: str, five: str) -> tuple[str, str, str]:
    """Marks the word forms for count(): "задача", "задачи", "задач"."""
    return one, two, five


def tr(text: str, /, **fields: object) -> str:
    translated = vocabulary(current()).get(text, text)
    return translated.format(**fields) if fields else translated


def count(n: int, forms: tuple[str, str, str]) -> str:
    """In Russian "1 задача", "2 задачи", "5 задач"; in English "1 task", "5 tasks": the forms come from the
    dictionary by the key "задача|задачи|задач" with the value "task|tasks"."""
    if current() != "ru":
        one, many = vocabulary(current()).get("|".join(forms), f"{forms[0]}|{forms[2]}").split("|")
        return f"{n} {one if n == 1 else many}"
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {forms[0]}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {forms[1]}"
    return f"{n} {forms[2]}"
