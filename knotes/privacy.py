# SPDX-License-Identifier: GPL-3.0-only
"""Personal data hidden from a cloud model (152-ФЗ): before the minutes go to a cloud service, phones, e-mails, INN,
SNILS, passports, card and account numbers and street addresses in the model input become labels like [ТЕЛЕФОН_1];
people's names too if the user asks. After the answer the labels go back to the real values on this computer: the
minutes on disk have the full text.

The same value keeps its label within a meeting. For the user's own AI assistant over MCP the table of labels is kept
in the meeting folder (LABELS) and is never given to the model. Numbers are matched by their exact shape and check
digits, so dates, times, sums, versions and task numbers stay. Audio sent to cloud speech recognition cannot be hidden:
only the text going to the minutes model is.
"""

import bisect
import dataclasses
import ipaddress
import pathlib
import re
import urllib.parse
from collections.abc import Callable, Iterable

from knotes import catalog, transcript
from knotes import vocabulary as vocab

LABELS = "скрытые_данные.json"  # label -> real value, in the meeting folder
NOTE = (
    "Метки в квадратных скобках вида [ТЕЛЕФОН_1], [EMAIL_2], [ЧЕЛОВЕК_3] - скрытые личные данные. Переносите их в "
    "протокол как есть, буква в букву, не расшифровывайте и не придумывайте вместо них значения."
)
PERSON = "ЧЕЛОВЕК"
LABEL = re.compile(r"\[\s*([A-Za-zА-Яа-яЁё]+)_(\d+)\s*\]")
# case endings of a name: "Анн" + "ой", "Петров" + "ым"
ENDINGS = (
    r"(?:ами|ями|ого|его|ому|ему|ыми|ими|ой|ей|ою|ею|ом|ем|ым|им|ую|ая|ое|ые|ых|их|ам|ям|ах|ях|ов|ев|ью|а|я|ы|и|е|у|ю"
    r"|ь)?"
)
SURNAME = re.compile(
    r"^[А-ЯЁ][а-яё]+(?:ов|ев|ёв|ин|ын|ова|ева|ёва|ина|ына|ский|ская|цкий|цкая|енко|ук|юк|ич|ян|дзе|швили)$"
)
SHORT_NAME = 3  # shorter words of a name are hidden only as part of the whole name: "Ян" alone is too ambiguous
STREET = (
    r"(?:ул\.|улиц[аеуы]|просп\.|пр-т|проспект[ае]?|пер\.|переул(?:ок|ке)|ш\.|шоссе|наб\.|набережн(?:ая|ой)|"
    r"бульвар[ае]?|б-р|пл\.|площад[ьи])"
)
ADDRESS = (
    rf"(?<!\w){STREET}\s+[\w.-]+(?:\s+[\w.-]+){{0,2}},?\s+(?:дом|д\.?)\s*\d+[\w/-]*"
    r"(?:,?\s*(?:корп\.|корпус|к\.|стр\.|строение)\s*\d+\w*)?(?:,?\s*(?:кв\.|квартира|оф\.|офис)\s*\d+\w*)?"
)
PASSPORT = r"\d{2} ?\d{2}[ ,]*(?:(?:номер|№)\s*)?\d{6}"


def luhn(number: str) -> bool:
    """Check digit of a card number."""
    total = 0
    for position, digit in enumerate(reversed(number)):
        doubled = int(digit) * (2 if position % 2 else 1)
        total += doubled - 9 if doubled > 9 else doubled
    return total % 10 == 0


def _check(digits: list[int], weights: tuple[int, ...]) -> int:
    return sum(w * d for w, d in zip(weights, digits, strict=False)) % 11 % 10


def inn(number: str) -> bool:
    """Check digits of an INN: 10 digits of a company, 12 of a person."""
    digits = [int(z) for z in number]
    if len(digits) == 10:
        return _check(digits, (2, 4, 10, 3, 5, 9, 4, 6, 8)) == digits[9]
    return (
        len(digits) == 12
        and _check(digits, (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)) == digits[10]
        and _check(digits, (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)) == digits[11]
    )


def snils(number: str) -> bool:
    """Check number of a SNILS: the last two of its 11 digits."""
    digits = [int(z) for z in number]
    total = sum(d * (9 - n) for n, d in enumerate(digits[:9]))
    control = total % 101 if total > 101 else total
    return (0 if control >= 100 else control) == digits[9] * 10 + digits[10]


def _phone_length(number: str) -> bool:
    return 10 <= len(number) <= 15


# (label kind, pattern, check of its digits); the first kind to claim a place keeps it. A pattern with a group hides
# only the group: the keyword before it stays.
PATTERNS: tuple[tuple[str, re.Pattern, Callable[[str], bool] | None], ...] = (
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,}"), None),
    ("КАРТА", re.compile(r"(?<!\d)\d{4}(?:[ -]?\d{4}){3}(?!\d)"), luhn),
    ("СЧЕТ", re.compile(r"(?<!\d)\d{20}(?!\d)"), None),
    ("ИНН", re.compile(r"\bИНН\b[^\d\n]{0,20}?(\d{12}|\d{10})(?!\d)", re.IGNORECASE), None),
    ("ИНН", re.compile(r"(?<!\d)(?:\d{12}|\d{10})(?!\d)"), inn),
    ("СНИЛС", re.compile(r"\bСНИЛС\b[^\d\n]{0,20}?(\d{3}[ -]?\d{3}[ -]?\d{3}[ -]?\d{2})(?!\d)", re.IGNORECASE), None),
    ("СНИЛС", re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}[ -]\d{2}(?!\d)"), None),
    ("СНИЛС", re.compile(r"(?<!\d)\d{11}(?!\d)"), snils),
    ("ПАСПОРТ", re.compile(rf"паспорт\w*[^\d\n]{{0,20}}?({PASSPORT}|\d{{10}})(?!\d)", re.IGNORECASE), None),
    ("ПАСПОРТ", re.compile(r"(?<![\d.,])\d{2} ?\d{2} \d{6}(?![\d.,]?\d)"), None),
    ("ТЕЛЕФОН", re.compile(r"(?<![\w.,:+])(?:\+7|8)[ (-]*\d{3}[ )-]*\d{3}[ -]?\d{2}[ -]?\d{2}(?!\d)"), None),
    ("ТЕЛЕФОН", re.compile(r"(?<![\w+])\+(?!7)\d{1,3}(?:[ -]?\(?\d{1,4}\)?){2,5}(?!\d)"), _phone_length),
    ("ТЕЛЕФОН", re.compile(r"(?<![\d.,:+])9\d{2}[ -]?\d{3}[ -]?\d{2}[ -]?\d{2}(?!\d)"), None),
    ("ТЕЛЕФОН", re.compile(r"(?<![\d.,:-])\d{3}-\d{2}-\d{2}(?![\d-])"), None),
    ("АДРЕС", re.compile(ADDRESS, re.IGNORECASE), None),
)


@dataclasses.dataclass(frozen=True)
class _Place:
    start: int
    end: int
    kind: str
    value: str  # what is restored: the text found, for a person the whole name
    key: str  # the same key, the same label


def _key(kind: str, value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if kind == "ТЕЛЕФОН":
        return digits[-10:]
    if kind in ("EMAIL", "АДРЕС"):
        return " ".join(value.lower().split())
    return value if kind == PERSON else digits


def _name_word(word: str) -> str:
    """A word of a name with its case endings; the final vowel of the nominative goes: "Анна" -> "Анн" + ending."""
    if len(word) < SHORT_NAME or not re.fullmatch(r"[А-Яа-яЁё-]+", word):
        return re.escape(word)
    stem = word[:-1] if word[-1] in "аяьйоеыи" and len(word) > SHORT_NAME else word
    return f"(?:{re.escape(word)}|{re.escape(stem)}{ENDINGS})"


def _person_patterns(people: Iterable[str]) -> list[tuple[str, re.Pattern]]:
    """(whole name, pattern): the whole name first, then each of its words that belongs to this person only."""
    names = list(dict.fromkeys(p.strip() for p in people if p.strip()))
    words = {name: re.findall(r"[^\W\d_][\w-]*", name) for name in names}
    owners: dict[str, int] = {}
    for name_words in words.values():
        for word in set(name_words):
            owners[word] = owners.get(word, 0) + 1
    result = [
        (name, re.compile(r"(?<!\w)" + r"\s+".join(map(_name_word, words[name])) + r"(?!\w)"))
        for name in names
        if words[name]
    ]
    result += [
        (name, re.compile(rf"(?<!\w){_name_word(word)}(?!\w)"))
        for name in names
        if len(words[name]) > 1
        for word in words[name]
        if owners[word] == 1 and len(word) >= SHORT_NAME
    ]
    return result


def _split(label: str) -> tuple[str, int]:
    """ "[ТЕЛЕФОН_2]" -> ("ТЕЛЕФОН", 2)."""
    kind, _, number = label.strip("[]").rpartition("_")
    return kind, int(number) if number.isdigit() else 0


@dataclasses.dataclass
class Hider:
    """Hides personal data in text and puts it back; the table (label -> value) is shared by the calls."""

    people: tuple[str, ...] = ()  # names to hide; empty - names stay
    table: dict[str, str] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        self._persons = _person_patterns(self.people)
        self._labels = {_key(_split(label)[0], value): label for label, value in self.table.items()}
        self._numbers: dict[str, int] = {}  # the last number of each kind
        for kind, number in map(_split, self.table):
            self._numbers[kind] = max(self._numbers.get(kind, 0), number)

    def _places(self, text: str) -> list[_Place]:
        """Places to hide in text order; they do not overlap, kept sorted by start to check a new one quickly."""
        taken: list[_Place] = []
        starts: list[int] = []
        found = [(kind, pattern, check, None) for kind, pattern, check in PATTERNS]
        found += [(PERSON, pattern, None, name) for name, pattern in self._persons]
        for kind, pattern, check, name in found:
            for match in pattern.finditer(text):
                group = 1 if pattern.groups else 0
                start, end, value = match.start(group), match.end(group), match.group(group)
                if check and not check(re.sub(r"\D", "", value)):
                    continue
                at = bisect.bisect_left(starts, start)
                if (at and taken[at - 1].end > start) or (at < len(taken) and taken[at].start < end):
                    continue
                starts.insert(at, start)
                taken.insert(at, _Place(start, end, kind, name or value, _key(kind, name or value)))
        return taken

    def _label(self, place: _Place) -> str:
        if place.key not in self._labels:
            self._numbers[place.kind] = self._numbers.get(place.kind, 0) + 1
            label = f"[{place.kind}_{self._numbers[place.kind]}]"
            self.table[label], self._labels[place.key] = place.value, label
        return self._labels[place.key]

    def hide(self, text: str) -> str:
        parts, position = [], 0
        for place in self._places(text):
            parts += [text[position : place.start], self._label(place)]
            position = place.end
        return "".join([*parts, text[position:]])

    def with_note(self, text: str) -> str:
        """Hidden text for the model input, with the note about the labels if anything was hidden."""
        hidden = self.hide(text)
        return f"{NOTE}\n\n{hidden}" if hidden != text else text

    def restore(self, text: str) -> str:
        """Labels back to the real values; a label the table does not know stays as it is."""
        return LABEL.sub(lambda m: self.table.get(f"[{m.group(1).upper()}_{m.group(2)}]", m.group(0)), text)


def cloud(address: str) -> bool:
    """The service is not on this computer or in its local network (Ollama, LM Studio, an own server)."""
    host = (urllib.parse.urlsplit(address.strip()).hostname or "").lower()
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." in host and not host.endswith((".local", ".lan", ".home", ".localhost"))
    return not (ip.is_loopback or ip.is_private or ip.is_link_local)


def people(folder: pathlib.Path | None, my_name: str, words: Iterable[vocab.Word]) -> tuple[str, ...]:
    """Names to hide in a meeting: the owner's name, participants from the calendar, speaker names and the surnames of
    the vocabulary with their misheard variants; with no meeting (the meeting prep) the owner and the vocabulary."""
    surnames = [z for word in words if SURNAME.match(word.correct.strip()) for z in (word.correct, *word.variants)]
    calendar = catalog.properties(folder).get("участники_календаря", []) if folder else []
    speakers = transcript.names(folder).values() if folder else ()
    found = [my_name, *(calendar if isinstance(calendar, list) else []), *speakers, *surnames]
    names = (z.strip() for z in found if isinstance(z, str))
    return tuple(dict.fromkeys(z for z in names if len(re.sub(r"\W", "", z)) >= 2 and z != transcript.ME))


def table(folder: pathlib.Path) -> dict[str, str]:
    """The meeting's labels from earlier tasks: the same value keeps its label."""
    return {k: z for k, z in transcript.read_json(folder / LABELS, {}).items() if isinstance(z, str)}


def remember(folder: pathlib.Path, hider: Hider) -> None:
    """The table stays in the meeting folder, on this computer: save_protocol restores the values by it. Labels another
    call saved meanwhile stay too."""
    if hider.table and hider.table != table(folder):
        transcript.update_json(folder / LABELS, {}, lambda saved: saved | hider.table)


def restored(folder: pathlib.Path, text: str) -> str:
    return Hider(table=table(folder)).restore(text)
