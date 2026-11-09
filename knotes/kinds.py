# SPDX-License-Identifier: GPL-3.0-only
"""Meeting kinds: discovery, demo, spec review and others each have their own minutes sections.

Sections and kinds are data (meeting_kinds.json): how to write an item, what goes into the section, the block shade,
whether an item has "who and when" and whether it goes to the task board. The kind is a meeting property; if it is not
set, the default kind of the meeting's project is used, otherwise a working meeting.
"""

import dataclasses
import functools
import json
import pathlib

from knotes import catalog, projects

DESCRIPTIONS = pathlib.Path(__file__).with_name("meeting_kinds.json")
WORKING = "Рабочая встреча"
SHARED = ("Следующая встреча", "Для аналитика")  # at the end of the minutes of any kind


@dataclasses.dataclass(frozen=True)
class Description:
    heading: str
    item: str
    what: str = ""
    shade: str = "серый"
    who: bool = False  # the last sentence of the item is who and when
    board: str = ""  # the item goes to the task board: task or question
    account: tuple[str, ...] = ()  # for the result in the meeting list: "1 замечание", "2 замечания", "5 замечаний"
    names: dict[str, str] = dataclasses.field(default_factory=dict)  # heading in other languages: {"en": "Tasks"}


@dataclasses.dataclass(frozen=True)
class Kind:
    name: str
    description: str
    sections: tuple[str, ...]
    instruction_text: str = ""  # what else to tell the model about this kind


@functools.cache
def _data() -> dict:
    return json.loads(DESCRIPTIONS.read_text(encoding="utf-8"))


@functools.cache
def descriptions() -> dict[str, Description]:
    return {
        z: Description(
            z,
            d["пункт"],
            d.get("что", ""),
            d.get("оттенок", "серый"),
            d.get("кто", False),
            d.get("доска", ""),
            tuple(d.get("счет", ())),
            d.get("имена", {}),
        )
        for z, d in _data()["разделы"].items()
    }


@functools.cache
def _headings() -> tuple[tuple[str, Description], ...]:
    return tuple((z.casefold(), o) for o in descriptions().values() for z in (o.heading, *o.names.values()))


def description(heading: str) -> Description:
    """Minutes section by heading in any language, case aside: "Задачи до релиза" and "Open Questions" are known
    sections; an unknown section is grey, without "who"."""
    folded = heading.casefold()
    return next((o for z, o in _headings() if folded.startswith(z)), Description(heading, ""))


@functools.cache
def kinds() -> tuple[Kind, ...]:
    return tuple(Kind(d["имя"], d["описание"], tuple(d["разделы"]), d.get("указание", "")) for d in _data()["виды"])


def kind(name: str | None) -> Kind:
    """An unknown name (the kind no longer exists) gives a working meeting."""
    return next((v for v in kinds() if v.name == name), kinds()[0])


def meeting_kind(folder: pathlib.Path) -> Kind:
    properties = catalog.properties(folder)
    return kind(properties.get("вид") or projects.kind(folder.parent, properties.get("проект", "")))


def for_model(kind_: Kind, language: str) -> list[str]:
    """Model input lines: the meeting kind and its sections in order, how to write an item and what goes into it.
    Headings are in the language of the minutes, with the Russian one the instruction refers to."""
    lines = [f"Вид встречи - {kind_.name}. {kind_.description}", "", "Разделы протокола по порядку"]
    for heading in kind_.sections + SHARED:
        o = descriptions()[heading]
        name = o.names.get(language, heading)
        title = heading if name == heading else f"{name} («{heading}»)"
        lines.append(f"- {title}. Пункт «{o.item}»." + (f" {o.what}" if o.what else ""))
    return lines + ([kind_.instruction_text] if kind_.instruction_text else [])
