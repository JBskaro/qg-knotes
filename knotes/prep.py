# SPDX-License-Identifier: GPL-3.0-only
"""Meeting prep with AI: questions for the client and a checklist of what to prepare or request before the meeting.

A first meeting (a discovery, say) has no history to build the agenda from. The minutes model gets what is known
about the project (About the project, the meeting kind with its sections, the next meeting in the calendar, the prep
typed so far and the open questions of past meetings) and suggests questions by the topics of the meeting kind and a
short checklist, by the instruction in prep_prompt.md. Its lines are added to the prep under the analyst's text,
without repeating what is already there or in the agenda; from the prep they go to the recording notepad as usual.

The request cost is not in the spending log yet: the log takes only meeting expenses (spending.write).
"""

import dataclasses
import datetime
import logging
import pathlib
import re
from collections.abc import Iterable, Sequence

from knotes import catalog, ics_calendar, kinds, minutes, notes, projects, providers, settings, spending, tasks
from knotes.language import tr

INSTRUCTION = pathlib.Path(__file__).with_name("prep_prompt.md")
LIST_MARK = re.compile(r"^(?:[-*•]\s+|\d+[.)]\s+|\[[ xX]?\]\s+)+")  # "1. ", "- ", "[ ] " before a line of the answer
TOPIC = re.compile(r"^([^?\-]{1,40}) - (.+)$")  # "Объемы - Сколько позиций номенклатуры?"
log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Past:
    """What the project's past meetings bring to the agenda. Collecting it reads every meeting and the board, so the
    project page does it once, not on every edit of the prep."""

    undiscussed: tuple[str, ...] = ()  # agenda items of the last meeting that were not reached
    from_board: tuple[str, ...] = ()  # open questions and decisions awaiting confirmation
    meetings: int = 0


@dataclasses.dataclass(frozen=True)
class Suggestion:
    lines: tuple[str, ...]  # for the prep: questions and the checklist, none of them in the agenda yet
    cost: float = 0.0


def past(meetings: Sequence[catalog.Meeting], project_tasks: Iterable[tasks.Task]) -> Past:
    """From the project's meetings (newest first, as catalog.list_ gives them) and its board cards."""
    last = next((v for v in meetings if v.folder.is_dir()), None)
    undiscussed = tuple(p.text for p in notes.read(last.folder).agenda if not p.discussed) if last else ()
    from_board = tuple(z.text for z in project_tasks if z.kind != tasks.TASK and z.opened)
    return Past(undiscussed, from_board, len(meetings))


def _lines(text: str) -> list[str]:
    return [s for s in (s.strip().removeprefix("- ").strip() for s in text.splitlines()) if s]


def agenda(prep_text: str, info_text: str, past_: Past) -> list[notes.AgendaItem]:
    """The agenda as tasks.agenda builds it from the saved files, here from the texts on the page: the prep, the
    questions in About the project, what was not discussed last time and the board, without repeats."""
    questions = [*_lines(prep_text), *(s for s in _lines(info_text) if notes.question(s))]
    unique: dict[str, str] = {}
    for text in (*questions, *past_.undiscussed, *past_.from_board):
        unique.setdefault(text.casefold().rstrip(".?! "), text)
    unique.pop("", None)
    return [notes.AgendaItem(z) for z in unique.values()]


def answer_language(*texts: str) -> minutes.Language:
    """The language of the first text with letters (About the project, then the prep), counted as minutes.language_of
    counts the speech; Russian without letters or on a tie."""
    text = next((s for s in texts if any(z.letters.search(s) for z in minutes.languages())), "")
    return max(minutes.languages(), key=lambda z: len(z.letters.findall(text)))


def meeting_kind(recordings: pathlib.Path, project: str, title: str = "") -> kinds.Kind:
    """The project's default kind; not set - the kind named in the meeting title ("Обследование склада"), otherwise
    a working meeting."""
    folded = title.casefold()
    named = (v.name for v in kinds.kinds() if folded and any(z.casefold() in folded for z in (v.name, tr(v.name))))
    return kinds.kind(projects.kind(recordings, project) or next(named, ""))


def calendar() -> list[ics_calendar.Event]:
    """Meetings of the coming week by the calendar link; none without a link or when the calendar cannot be read: the
    prep goes on without it."""
    url = settings.key(ics_calendar.KEY)
    if not url:
        return []
    try:
        return ics_calendar.upcoming(url)
    except Exception as error:  # noqa: BLE001 - the calendar only adds the meeting title: the prep goes on without it
        log.warning("calendar not read for meeting prep: %s", error)
        return []


def next_meeting(events: Iterable[ics_calendar.Event], project: str, titles: set[str]) -> ics_calendar.Event | None:
    """The project's nearest meeting: one of its series (a past meeting had that title) or with the project name in
    the title."""
    name = project.casefold()
    return next((s for s in events if s.title in titles or (name and name in s.title.casefold())), None)


def input_(recordings: pathlib.Path, project: str, past_: Past, upcoming: ics_calendar.Event | None = None) -> str:
    """What the model gets: the project, how many meetings it had, the next meeting, the answer language, the meeting
    kind with its sections, About the project, the prep and the open items of past meetings."""
    info, prep_text = projects.notes(recordings, project).text, projects.for_meeting(recordings, project)
    language_ = answer_language(info, prep_text)
    lines = [f"Проект - {project}", f"Прошлых встреч по проекту - {past_.meetings}"]
    if upcoming:
        lines.append(f"Ближайшая встреча - {upcoming.title}, {upcoming.start:%d.%m.%Y %H:%M}")
    kind_ = meeting_kind(recordings, project, upcoming.title if upcoming else "")
    lines += [f"Язык ответа - {language_.name}", "", *kinds.for_model(kind_, language_.code)]
    if info.strip():
        lines += ["", "О проекте", info.strip()]
    for heading, items in (
        ("Уже в подготовке", _lines(prep_text)),
        ("Открытые вопросы и решения на подтверждении с прошлых встреч", [*past_.undiscussed, *past_.from_board]),
    ):
        if items:
            lines += ["", heading, *(f"- {s}" for s in dict.fromkeys(items))]
    return "\n".join(lines) + "\n"


def _ready(params_: minutes.Params) -> None:
    """The same settings check as for the minutes: nothing is sent without a key, an address or a model."""
    s = params_.service
    missing = (
        tr("No API key for {service}, set it in the Minutes section in Settings", service=s.title)
        if s.key_needed and not params_.key
        else tr("No address or model set for {service}, check the Minutes section in Settings", service=s.title)
        if not s.address or not params_.model
        else ""
    )
    if missing:
        raise providers.ProviderError(missing)


def suggest(recordings: pathlib.Path, project: str, params_: minutes.Params) -> Suggestion:
    """Asks the minutes model (its service and model, our own instruction) for questions and a checklist."""
    _ready(params_)
    meetings = [v for v in catalog.list_(recordings) if v.project == project]
    past_ = past(meetings, tasks.tasks(recordings, project))
    upcoming = next_meeting(calendar(), project, {v.custom_title for v in meetings if v.custom_title})
    hider = minutes.hider_for(None, params_)
    content = input_(recordings, project, past_, upcoming)
    body: dict = {
        "model": params_.model,
        "messages": [
            {"role": "system", "content": INSTRUCTION.read_text(encoding="utf-8")},
            {"role": "user", "content": hider.with_note(content) if hider else content},
        ],
    }
    if params_.service.kind == "openrouter":
        body["provider"] = {"data_collection": "allow" if params_.retention else "deny"}
    answer, cost = providers.model_answer(params_.service, params_.key, body)
    answer = hider.restore(answer) if hider else answer
    _log_cost(recordings, project, params_.model, cost)
    info = projects.notes(recordings, project).text
    known = [p.text for p in agenda(projects.for_meeting(recordings, project), info, past_)]
    lines = new_lines(answer, known)
    log.info("meeting prep by %s: %d new lines, cost %.5f $", params_.model, len(lines), cost)
    return Suggestion(tuple(lines), cost)


def _log_cost(recordings: pathlib.Path, project: str, model: str, cost: float) -> None:
    """The prep is paid like the minutes; it has no meeting, so it goes to the log under the project."""
    try:
        spending.add(
            recordings,
            spending.Expense(
                datetime.datetime.now(), "", catalog.MINUTES_KIND, model, cost, project, tr("Meeting prep")
            ),
        )
    except OSError as error:  # the answer is paid and received: it is shown even if the log is not written
        log.warning("cost of the meeting prep of %s not logged: %s", project, error)


def _clean(line: str) -> str:
    """A line of the answer without list marks, bold and extra spaces: "1. **Объемы** - ...?" -> "Объемы - ...?"."""
    return " ".join(LIST_MARK.sub("", line.strip()).replace("**", "").split())


def _keys(line: str) -> set[str]:
    """What a line repeats by: the whole line and the line without its topic, case and end marks aside."""
    clean = _clean(line)
    topic = TOPIC.match(clean)
    return {k.casefold().rstrip(".?!… ") for k in (clean, topic[2] if topic else "")} - {""}


def _heading(line: str) -> bool:
    """A Markdown heading, a code-block fence, "Вопросы:" or a line in bold only, which are not agenda items."""
    text = line.strip()
    return text.startswith(("#", "```")) or text.endswith(":") or bool(re.fullmatch(r"\*\*[^*]+\*\*", text))


def new_lines(answer: str, known: Iterable[str]) -> list[str]:
    """Lines of the model's answer not in the known ones (the prep, the agenda) and not repeated, in the answer's
    order; headings, fences and empty lines are dropped."""
    seen: set[str] = set().union(*(_keys(s) for s in known))
    result = []
    for line in answer.splitlines():
        keys = _keys(line)
        if _heading(line) or not keys or keys & seen:
            continue
        seen |= keys
        result.append(_clean(line))
    return result


def addition(prep_text: str, lines: Iterable[str], known: Iterable[str] = ()) -> str:
    """What to append to the prep: the lines not in it or the known ones, each on its own line under the analyst's
    text, which stays as is."""
    fresh = new_lines("\n".join(lines), [*prep_text.splitlines(), *known])
    if not fresh:
        return ""
    return ("\n" if prep_text.strip() and not prep_text.endswith("\n") else "") + "\n".join(fresh)


def add_saved(recordings: pathlib.Path, project: str, lines: Iterable[str]) -> None:
    """The answer came after the project page closed: the lines go to the saved prep."""
    text = projects.for_meeting(recordings, project)
    projects.set_for_meeting(recordings, project, text + addition(text, lines))
