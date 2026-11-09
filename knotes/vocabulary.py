# SPDX-License-Identifier: GPL-3.0-only
"""Vocabulary: how a word is spelled correctly and how speech recognition may hear it.

Speech recognition confuses rare words ("Cloud" instead of "Claude"): the vocabulary fixes the transcript,
the correct spellings go to providers that accept words for accuracy, and they help when writing the minutes.
The word ending is kept, for Russian case endings: "игаиса" -> "ЕГАИСа".
"""

import dataclasses
import re

ENDPOINT = r"(\w{0,3})"  # case ending after the recognized variant
SHORT = 4  # shorter variants match whole words only, otherwise "cat" would fix "category"


@dataclasses.dataclass(frozen=True)
class Word:
    correct: str
    variants: tuple[str, ...] = ()


def from_data(data: list[dict]) -> list[Word]:
    """Vocabulary from the settings: [{"правильно": ..., "варианты": [...]}]."""
    return [Word(z["правильно"], tuple(z.get("варианты", ()))) for z in data if z.get("правильно", "").strip()]


def to_data(words: list[Word]) -> list[dict]:
    return [
        {"правильно": s.correct.strip(), "варианты": [v.strip() for v in s.variants if v.strip()]}
        for s in words
        if s.correct.strip()
    ]


def _template(variant: str) -> re.Pattern:
    ended = ENDPOINT if len(variant) >= SHORT else "()"
    return re.compile(rf"(?<!\w){re.escape(variant)}{ended}(?!\w)", re.IGNORECASE)


def fix(text: str, words: list[Word]) -> str:
    """Vocabulary variants are replaced with the correct spelling; the ending stays."""
    for word in words:
        for variant in word.variants:
            if variant.strip():
                text = _template(variant.strip()).sub(lambda n, p=word.correct: p + n.group(1), text)
    return text


def spellings(words: list[Word]) -> tuple[str, ...]:
    """Correct spellings for providers that accept words for accuracy."""
    return tuple(s.correct for s in words if s.correct.strip())
