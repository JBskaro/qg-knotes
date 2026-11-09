# SPDX-License-Identifier: GPL-3.0-only
"""Meeting minutes: a language model drafts them from the transcript by the instruction in minutes_prompt.md.

The draft lives in the meeting folder (протокол.md) and is edited by hand. For the email and the knowledge base the
time marks and the analyst-only section (FOR_ANALYST) are removed; the greeting goes only to the email, the link to
the transcript only to the knowledge base. A custom instruction can be set in the settings, it is stored next to the
program settings.

The minutes are written in the language of the meeting (minutes_languages.json): the instruction is Russian, for
another language the model gets its headings and template phrases, and the program reads minutes in any of them.
"""

import dataclasses
import functools
import json
import logging
import pathlib
import re
from collections.abc import Callable

from knotes import notes, catalog, privacy, projects, providers, transcript, spending, kinds, language
from knotes import settings
from knotes import vocabulary as vocab

FILE = "протокол.md"
DEFAULT_INSTRUCTION = pathlib.Path(__file__).with_name("minutes_prompt.md")
LANGUAGES = pathlib.Path(__file__).with_name("minutes_languages.json")
TIME, seconds_list = transcript.TIME, transcript.seconds_list  # time in the minutes is written as in the transcript
TIME_MARK = re.compile(rf" ?\[{TIME}\]")
NOTE_SIGN = re.compile(rf" ?{transcript.NOTE}\s*$")
FOR_ANALYST = "Для аналитика"  # section for us only: not sent to the client
# template lines of the instruction the program reads back, in every language of the minutes
PARTICIPANTS = "Участники - <имена через запятую>"
TRANSCRIPT_LINE = "Расшифровка - <имя папки встречи>, расшифровка.md"
TENTATIVE = "Предварительно, <кто подтвердит>."  # last phrase of a decision still to be confirmed
log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Section:
    heading: str
    text: str
    item_count: int  # numbered ones: decisions, tasks, questions


@dataclasses.dataclass(frozen=True)
class Item:
    number: str  # empty for a line without a number
    text: str  # without the time mark
    who: str = ""  # for tasks and questions: who and when
    second: int | None = None  # position in the recording by the time mark
    from_note: bool = False  # the item came from an analyst's note: "... [02:01] ✎"


@dataclasses.dataclass(frozen=True)
class Params:
    service: providers.Provider
    key: str = dataclasses.field(repr=False)  # the API key stays out of the log and debug output
    model: str
    instruction: str
    my_name: str = ""
    vocabulary: tuple[vocab.Word, ...] = ()
    retention: bool = False
    hide_data: bool = True  # personal data goes to a cloud model as labels
    hide_names: bool = False


@dataclasses.dataclass(frozen=True)
class Language:
    code: str
    name: str  # for the model: "английский"
    letters: re.Pattern  # letters of its alphabet
    phrases: dict[str, str]  # the instruction's template phrases in this language; none for Russian
    style: str = ""  # quotes and numbers in this language

    def phrase(self, russian: str) -> str:
        return self.phrases.get(russian, russian)


@functools.cache
def languages() -> tuple[Language, ...]:
    """Russian first: the instruction's language."""
    data = json.loads(LANGUAGES.read_text(encoding="utf-8"))
    return tuple(
        Language(z, d["имя"], re.compile(f"[{d['буквы']}]", re.IGNORECASE), d.get("фразы", {}), d.get("стиль", ""))
        for z, d in data.items()
    )


def language_of(folder: pathlib.Path) -> Language:
    """The language of the meeting: the one whose letters most of the speech is written in, Russian on a tie."""
    speech = " ".join(f["текст"] for f in (transcript.phrases(folder) or {}).get("фразы", []))
    return max(languages(), key=lambda z: len(z.letters.findall(speech)))


def _language_lines(language_: Language) -> list[str]:
    lines = [f"Язык протокола - {language_.name}"]
    if language_.phrases:
        lines += ["Фразы шаблона на этом языке", *(f"- «{z}» - «{o}»" for z, o in language_.phrases.items())]
    return lines + ([f"Стиль на этом языке - {language_.style}"] if language_.style else [])


@functools.cache
def _starts(template: str, end: str) -> tuple[str, ...]:
    """The start of a template line up to its end mark, in every language: "Участники", "Participants"."""
    return tuple(z.phrase(template).partition(end)[0] for z in languages())


def participants(line: str) -> tuple[str, str] | None:
    """(label, names) of the participants line in any language of the minutes; None for another line."""
    return next(((z, line[len(z) + 3 :]) for z in _starts(PARTICIPANTS, " - ") if line.startswith(f"{z} - ")), None)


def transcript_line(line: str) -> bool:
    return line.startswith(tuple(f"{z} - " for z in _starts(TRANSCRIPT_LINE, " - ")))


def tentative(who: str) -> str | None:
    """Who confirms a tentative decision: "Предварительно, Анна подтвердит." gives "Анна подтвердит."; None if the
    phrase is not that."""
    return next((who.removeprefix(z).lstrip(" ,") for z in _starts(TENTATIVE, ",") if who.startswith(z)), None)


def for_analyst(heading: str) -> bool:
    return kinds.description(heading).heading == FOR_ANALYST


def instruction_file() -> pathlib.Path:
    return settings.data_folder() / DEFAULT_INSTRUCTION.name


def instruction() -> str:
    """The custom instruction if one is set in the settings, otherwise the default one."""
    custom_one = instruction_file()
    return (custom_one if custom_one.exists() else DEFAULT_INSTRUCTION).read_text(encoding="utf-8")


def set_instruction(text: str) -> None:
    """An empty one or one equal to the default means back to the default: it is updated with the program."""
    custom_one = instruction_file()
    if not text.strip() or text.strip() == DEFAULT_INSTRUCTION.read_text(encoding="utf-8").strip():
        custom_one.unlink(missing_ok=True)
        return
    custom_one.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(custom_one, text.strip() + "\n")


def params(config: settings.Settings) -> Params:
    s = providers.connection("протоколы", config.minutes_service, config.minutes_address)
    return Params(
        s,
        settings.address_key(s.address, s.key, s.variable),
        config.minutes_model_id.strip() or s.model,
        instruction(),
        config.my_name.strip(),
        tuple(vocab.from_data(config.vocabulary)),
        config.provider_retention,
        config.hide_personal_data,
        config.hide_names,
    )


def input_(folder: pathlib.Path, params_: Params, kind: str | None = None, with_transcript: bool = True) -> str:
    """What the model gets: date, title, project, the owner's name, participants from the calendar, the language of
    the minutes, the meeting kind with its sections, vocabulary, project notes, the analyst's agenda and notes, and the
    transcript itself. kind: this meeting kind instead of the saved one."""
    properties = catalog.properties(folder)
    language_ = language_of(folder)
    lines = [f"Дата встречи - {catalog.folder_date(folder):%d.%m.%Y}"]
    for row_label, value in (
        ("Название встречи", properties.get("название")),
        ("Проект", properties.get("проект")),
        ("Имя владельца", params_.my_name),
        ("Участники по календарю", ", ".join(properties.get("участники_календаря", []))),
    ):
        if value:
            lines.append(f"{row_label} - {value}")
    kind_ = kinds.kind(kind) if kind else kinds.meeting_kind(folder)
    lines += ["", *_language_lines(language_), "", *kinds.for_model(kind_, language_.code)]
    if params_.vocabulary:
        lines += ["", "Словарь терминов"]
        lines += [
            f"- {s.correct}" + (f" (может распознаться как {', '.join(s.variants)})" if s.variants else "")
            for s in params_.vocabulary
        ]
    info = projects.notes(folder.parent, properties.get("проект", "")) if properties.get("проект") else None
    if info and info.text and info.for_model:
        lines += ["", "Заметки по проекту", info.text]
    lines += notes.for_model(folder)
    head = "\n".join(lines) + "\n"
    return head + "\n" + (folder / transcript.TRANSCRIPT).read_text(encoding="utf-8") if with_transcript else head


def _unwrapped(text: str) -> str:
    """The model sometimes wraps the answer in a ```markdown ... ``` block: only the content goes to the draft."""
    found = re.fullmatch(r"\s*```(?:markdown|md)?\s*\n(.*?)\n```\s*", text, flags=re.DOTALL)
    return (found.group(1) if found else text).strip() + "\n"


def _recognized(folder: pathlib.Path) -> None:
    if not (folder / transcript.TRANSCRIPT).exists():
        raise FileNotFoundError(language.tr("Meeting {name} isn't transcribed yet", name=folder.name))


def job(folder: pathlib.Path, params_: Params, kind: str | None = None) -> dict:
    """What the user's own AI assistant gets to write the minutes: the same instruction and input as the service,
    without the transcript: the assistant reads it in pieces, a whole one does not fit its answer limit. Nothing is
    written: kind is the meeting kind for this task only."""
    _recognized(folder)
    return {"instruction": params_.instruction, "input": input_(folder, params_, kind, with_transcript=False)}


def assistant_request(folder: pathlib.Path) -> str:
    """The request the user pastes to the AI assistant."""
    return language.tr(
        'Write the minutes of the meeting "{meeting}" from QG Knotes (folder {folder}): get the task with the '
        "get_protocol_task tool and save the minutes with the save_protocol tool.",
        meeting=catalog.display_title(folder),
        folder=folder.name,
    )


def save(folder: pathlib.Path, text: str, model: str, cost: float = 0.0) -> pathlib.Path:
    """The draft into the meeting folder without a code-block wrapper; who wrote it and what it cost."""
    file = folder / FILE
    transcript.write_text(file, _unwrapped(text))
    catalog.set_(folder, minutes_cost=round(cost, 5) or None, minutes_model=model)
    return file


def compose(folder: pathlib.Path, params_: Params, keep: Callable[[pathlib.Path], None] | None = None) -> pathlib.Path:
    """Draft minutes into the meeting folder; the request cost goes to the spending log and the meeting properties.
    keep(folder) runs after a successful answer, before the draft is replaced: the previous one is kept only then.
    The paid answer is written first: a failure of the spending log does not lose it."""
    s = params_.service
    _recognized(folder)
    if s.key_needed and not params_.key:
        raise providers.ProviderError(
            language.tr("No API key for {service}, set it in the Minutes section in Settings", service=s.title)
        )
    if not s.address or not params_.model:
        raise providers.ProviderError(
            language.tr("No address or model set for {service}, check the Minutes section in Settings", service=s.title)
        )
    notes.move_old(folder)  # before the transcript is read: old notes leave it for the notepad
    hider = hider_for(folder, params_)
    content = input_(folder, params_)
    body: dict = {
        "model": params_.model,
        "messages": [
            {"role": "system", "content": params_.instruction},
            {"role": "user", "content": hider.with_note(content) if hider else content},
        ],
    }
    if s.kind == "openrouter":
        body["provider"] = {"data_collection": "allow" if params_.retention else "deny"}
    text, cost = providers.model_answer(s, params_.key, body)
    return _paid(folder, hider.restore(text) if hider else text, params_.model, cost, keep)


def _paid(
    folder: pathlib.Path, text: str, model: str, cost: float, keep: Callable[[pathlib.Path], None] | None
) -> pathlib.Path:
    """A paid answer goes to the file first; the previous copy, the spending log and the price after it, and their
    failure does not lose it. The copy and the write are one edit under the meeting folder lock: an AI edit of the
    minutes at the same moment comes before or after it."""
    file = folder / FILE
    with transcript.locked(file):
        try:
            if keep:
                keep(folder)
        except OSError:
            log.exception("the previous minutes of %s are not kept", folder.name)
        transcript.write_text(file, _unwrapped(text))
    try:
        spending.write(folder, catalog.MINUTES_KIND, model, cost)  # before the new cost goes to the meeting
    except OSError:
        log.exception("the minutes expense of %s is not in the spending log", folder.name)
    catalog.set_(folder, minutes_cost=round(cost, 5) or None, minutes_model=model)
    return file


def hider_for(folder: pathlib.Path | None, params_: Params) -> privacy.Hider | None:
    """Personal data is hidden only from a cloud model: Ollama, LM Studio or an own server get the text as is. With no
    meeting (the meeting prep) the names to hide are the owner's and the vocabulary's."""
    if not params_.hide_data or not privacy.cloud(params_.service.address):
        return None
    return privacy.Hider(privacy.people(folder, params_.my_name, params_.vocabulary) if params_.hide_names else ())


def result(folder: pathlib.Path) -> str:
    """For the row in the meeting list: "3 decisions · 5 tasks · 2 questions"; no minutes - empty."""
    file = folder / FILE
    if not file.exists():
        return ""
    account: dict[str, int] = {}
    for section in sections(file.read_text(encoding="utf-8"))[1]:
        name = kinds.description(section.heading).heading
        account[name] = account.get(name, 0) + section.item_count
    return " · ".join(
        language.count(account[z], o.account) for z, o in kinds.descriptions().items() if o.account and account.get(z)
    )


def sections(text: str) -> tuple[str, list[Section]]:
    """Start of the minutes (heading, greeting, participants) and the "## ..." sections in order, to show as blocks."""
    start, *parts = re.split(r"^## ", text, flags=re.MULTILINE)
    result = []
    for part in parts:
        heading, _, body = part.partition("\n")
        result.append(Section(heading.strip(), body.strip(), len(re.findall(r"^\d+\. ", body, flags=re.MULTILINE))))
    return start.strip(), result


def items(section: Section) -> list[Item]:
    """Section lines to show: number, text, who and when, position in the recording. "Who and when" is the last
    phrase of items in tasks, questions and other sections with an assignee, and for a decision it is
    "Предварительно, <who confirms>" (TENTATIVE) in any language."""
    with_assignee = kinds.description(section.heading).who
    result = []
    for line in (s.strip() for s in section.text.splitlines()):
        if not line:
            continue
        number, _, text = line.partition(". ") if re.match(r"\d+\. ", line) else ("", "", line)
        label = TIME_MARK.search(text)
        unlabeled = TIME_MARK.sub("", text)
        text, who = NOTE_SIGN.sub("", unlabeled).strip(), ""
        start, point, end = text.removesuffix(".").rpartition(". ")
        if number and point and (with_assignee or tentative(end) is not None):
            text, who = start + ".", end + "."
        result.append(
            Item(number, text, who, seconds_list(label) if label else None, bool(NOTE_SIGN.search(unlabeled)))
        )
    return result


def _clean_lines(text: str) -> list[str]:
    """Lines without remarks ("> ..."), time marks, the note sign and the FOR_ANALYST section."""
    result, ours = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            ours = for_analyst(line[3:])
        if not ours and not line.startswith(">"):
            result.append(NOTE_SIGN.sub("", TIME_MARK.sub("", line)).rstrip())
    return result


def _join(lines: list[str]) -> str:
    """Consecutive empty lines become one."""
    result: list[str] = []
    for line in lines:
        if line or (result and result[-1]):
            result.append(line)
    return "\n".join(result).strip() + "\n"


def for_email(text: str) -> str:
    return _join([s for s in _clean_lines(text) if not transcript_line(s)])


def for_knowledge_base(text: str) -> str:
    """Without the email opening: before the sections only the heading and the participants are left."""
    result, start = [], True
    for line in _clean_lines(text):
        start = start and not line.startswith("## ")
        if not start or not line or line.startswith("#") or participants(line):
            result.append(line)
    return _join(result)


def no_diarization(text: str) -> str:
    """Markdown for pasting where markup is not understood: without heading hashes and asterisks."""
    return re.sub(r"^#+ ", "", text, flags=re.MULTILINE).replace("**", "")
