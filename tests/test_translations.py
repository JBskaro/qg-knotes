# SPDX-License-Identifier: GPL-3.0-only
"""Interface translation: every string shown has a translation with the same placeholders, English strings into
Russian and Russian ones into English; no translation is left over; strings to translate are constants, not
f-strings."""

import ast
import json
import pathlib
import re
import string
import unittest

from knotes import language

ROOT = pathlib.Path(language.__file__).parent
CYRILLIC = re.compile("[А-Яа-яЁё]")
NOTE_MARKS = {"tr", "tr_key"}  # tr("...") translates, tr_key("...") marks a constant string


def _name(call_: ast.Call) -> str:
    function = call_.func
    return (
        function.id if isinstance(function, ast.Name) else function.attr if isinstance(function, ast.Attribute) else ""
    )


def code_lines(files: list[pathlib.Path] | None = None) -> tuple[set[str], list[str]]:
    """Strings to translate from the code (the whole package or only these files) and meeting_kinds.json; the second
    item is the places where tr() gets a non-constant string.

    Strings of given files that lack a translation: python -c "import pathlib; from tests.test_translations import *;
    print(sorted(code_lines([pathlib.Path('knotes/ui/summary_window.py')])[0] - language.vocabulary('en').keys()))"
    """
    found, errors = set(), []
    for file in files or ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name, first_one = _name(node), node.args[0]
            if name == "forms" and all(isinstance(a, ast.Constant) for a in node.args):
                found.add("|".join(a.value for a in node.args))
            elif name in NOTE_MARKS and isinstance(first_one, ast.Constant) and isinstance(first_one.value, str):
                found.add(first_one.value)
            elif name in NOTE_MARKS and isinstance(first_one, ast.JoinedStr):
                errors.append(f"{file.name}:{node.lineno}")
    kinds = json.loads((ROOT / "meeting_kinds.json").read_text(encoding="utf-8"))
    found |= {v[field] for v in kinds["виды"] for field in ("имя", "описание")}
    found |= {"|".join(r["счет"]) for r in kinds["разделы"].values() if "счет" in r}
    models_ = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))["модели"]
    found |= {
        m[field] for m in models_ if not m.get("служебная") for field in ("название", "пояснение") if m.get(field)
    }
    return found, errors


def _substitutions(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


class TestTranslation(unittest.TestCase):
    def test_each_string_has_translation(self):
        found, errors = code_lines()
        self.assertEqual(errors, [], "tr() and tr_key() take a constant string, placeholders by name")
        russian = {line for line in found if CYRILLIC.search(line)}
        english = found - russian
        for code, source, name in (("en", russian, "English"), ("ru", english, "Russian")):
            translations = language.vocabulary(code)
            self.assertEqual(sorted(source - translations.keys()), [], f"no {name} translation")
            self.assertEqual(sorted(translations.keys() - source), [], "translation of a string not in the code")
            for line, translated in translations.items():
                with self.subTest(line=line):
                    self.assertEqual(_substitutions(line), _substitutions(translated))
        for line, translated in language.vocabulary("en").items():
            self.assertEqual(line.count("|"), 2 * translated.count("|"))  # forms: three Russian, two English

    def test_language_and_number(self):
        try:
            language.set_("en")
            self.assertEqual(language.current(), "en")
            self.assertEqual(language.count(1, language.forms("участник", "участника", "участников")), "1 participant")
            self.assertEqual(language.count(3, language.forms("участник", "участника", "участников")), "3 participants")
            self.assertEqual(language.tr("Нет такой строки {n}", n=2), "Нет такой строки 2")  # no translation: Russian
            language.set_("xx")
            self.assertIn(language.current(), language.LANGUAGES)  # unknown code: the Windows language
        finally:
            language.set_("ru")
        self.assertEqual(language.count(21, language.forms("участник", "участника", "участников")), "21 участник")


if __name__ == "__main__":
    unittest.main()
