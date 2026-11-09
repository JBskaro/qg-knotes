# SPDX-License-Identifier: GPL-3.0-only
"""MCP tools for meetings: Claude and other clients see meetings, minutes, transcripts, open tasks and spending,
and at the user's request write and edit the minutes, fill in names and change the title.

A meeting is identified by the name of its folder in the recordings folder. Parameters are in Latin letters: that is
how Claude accepts them.
Run: "QG Knotes.exe --mcp": exchange over standard input and output, without network.
"""

import datetime
import json
import pathlib
import re
import sys

from knotes import (
    tasks,
    notes,
    catalog,
    failures,
    projects,
    minutes,
    providers,
    transcript,
    summary,
    system,
    spending,
    kinds,
    privacy,
)
from knotes import settings
from knotes import vocabulary as vocab
from knotes.mcp import Tool, ToolError, Prompt, serve

PREVIOUS = "протокол.прежний.md"  # minutes text before replacement: an AI edit can be rolled back
COPIES = 5  # how many previous minutes texts to keep: протокол.прежний.md, .2.md, ...
KINDS = {"полный": lambda text: text, "письмо": minutes.for_email, "база_знаний": minutes.for_knowledge_base}
INSTRUCTION = (
    "Встречи аналитика из программы QG Knotes («Узелок»): расшифровки и протоколы встреч с клиентами. "
    "Встреча обозначается именем папки - поле «встреча» в list_meetings. Даты - ГГГГ-ММ-ДД. Протокол - "
    "черновик: клиенту он уходит только после проверки аналитиком, раздел «Для аналитика» клиенту не "
    "отправлять. compose_protocol и update_protocol вызывать только по явной просьбе пользователя. "
    "Составить протокол своей моделью: get_protocol_task, затем save_protocol; compose_protocol - "
    "платный сервис самой программы."
)
ASSISTANT = "помощник ИИ (MCP)"  # who wrote the minutes when the client does not say its model
PIECE = 30000  # characters of a transcript piece: about 10 thousand tokens, within a client's answer limit


def _meeting(meeting: str) -> catalog.Meeting:
    """Meeting by folder name; foreign paths and unknown names get a clear refusal."""
    custom = bool(meeting) and meeting not in (".", "..") and pathlib.PureWindowsPath(meeting).name == meeting
    found = catalog.meeting(settings.load().recordings / meeting) if custom else None
    if found is None:
        raise ToolError(f"Нет встречи «{meeting}». Имена встреч - в list_meetings, поле «встреча».")
    return found


def _date(text: str | None, param: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(text) if text else None
    except ValueError:
        raise ToolError(f"{param}: дата в виде ГГГГ-ММ-ДД, например 2026-10-22") from None


def _meetings(project: str | None, date_from: str | None, date_to: str | None) -> list[catalog.Meeting]:
    """Meetings and recordings for the period (both dates inclusive), newest first; project keeps only that project."""
    meetings = [v for v in catalog.list_(settings.load().recordings) if project is None or v.project == project]
    by = _date(date_to, "date_to") or datetime.date(9998, 12, 31)
    return summary.for_period(meetings, _date(date_from, "date_from"), by + datetime.timedelta(days=1))


def _card(found: catalog.Meeting) -> dict:
    folder = found.folder
    ready = found.status == catalog.READY
    speaker_names = transcript.names(folder) if ready else {}
    has_minutes = (folder / minutes.FILE).exists()
    return {
        "встреча": folder.name,
        "название": found.heading,
        "дата": f"{found.date:%Y-%m-%d %H:%M}",
        "длительность_мин": round(found.seconds / 60),
        "проект": found.project,
        "состояние": found.status,
        "участники": [speaker_names.get(who, who) for who in transcript.participants(folder)] if ready else [],
        "протокол": (minutes.result(folder) or "есть") if has_minutes else None,
        "вид_встречи": kinds.meeting_kind(folder).name,
    }


def _hider(found: catalog.Meeting) -> privacy.Hider | None:
    """The user's own assistant is a cloud model too: with the setting on it gets personal data as labels, by the
    meeting's table of labels."""
    config = settings.load()
    if not config.hide_personal_data:
        return None
    words = vocab.from_data(config.vocabulary)
    people = privacy.people(found.folder, config.my_name, words) if config.hide_names else ()
    return privacy.Hider(people, privacy.table(found.folder))


def _hidden(found: catalog.Meeting, answer: dict, note: str = "") -> dict:
    """The answer with personal data as labels; the meeting folder name stays, it names the meeting for the tools.
    note: the field that gets the note about the labels."""
    hider = _hider(found)
    if hider is None:
        return answer
    result = {k: z if k == "встреча" else _hide_value(hider, z) for k, z in answer.items()}
    if note:
        result[note] = f"{privacy.NOTE}\n\n{result[note]}"
    privacy.remember(found.folder, hider)
    return result


def _hide_value(hider: privacy.Hider, value: object) -> object:
    if isinstance(value, str):
        return hider.hide(value)
    if isinstance(value, list):
        return [_hide_value(hider, z) for z in value]
    if isinstance(value, dict):
        return {k: _hide_value(hider, z) for k, z in value.items()}
    return value


def _lines(found: catalog.Meeting) -> list[tuple[float, str]]:
    try:
        return transcript.lines(found.folder)
    except FileNotFoundError:
        raise ToolError(f"Встреча «{found.folder.name}» еще не распознана") from None


# --- reading --------------------------------------------------------------------------------------------------


def list_meetings(
    project: str | None = None, date_from: str | None = None, date_to: str | None = None, limit: int = 50
) -> dict:
    meetings = _meetings(project, date_from, date_to)
    return {"встреч": len(meetings), "встречи": [_card(v) for v in meetings[:limit]]}


def search_meetings(
    query: str, project: str | None = None, date_from: str | None = None, date_to: str | None = None, limit: int = 20
) -> dict:
    """Where it was found and up to three transcript excerpts with time."""
    if not query.strip():
        raise ToolError("query: что искать")
    meetings = _meetings(project, date_from, date_to)
    found, search_text = catalog.find(meetings, query), catalog.for_search(query.strip())
    result = [
        {
            **_card(v),
            "совпадений": found[v.folder],
            "отрывки": [
                f"[{transcript.time(s)}] {tr}"
                for s, tr in (_lines(v) if v.status == catalog.READY else [])
                if search_text in catalog.for_search(tr)
            ][:3],
        }
        for v in [v for v in meetings if v.folder in found][:limit]
    ]
    return {"запрос": query, "встреч_найдено": len(found), "найдено": result}


def get_protocol(meeting: str, view: str = "полный") -> dict:
    found = _meeting(meeting)
    file = found.folder / minutes.FILE
    if not file.exists():
        raise ToolError("У встречи нет протокола. Составить его - compose_protocol, по просьбе пользователя.")
    if view not in KINDS:
        raise ToolError(f"view: один из {', '.join(KINDS)}")
    return {**_card(found), "вид": view, "текст": KINDS[view](file.read_text(encoding="utf-8"))}


def _seconds_of(text: str | None, param: str, by_default: float) -> float:
    if not text:
        return by_default
    found = re.fullmatch(minutes.TIME, text.strip())
    if found is None:
        raise ToolError(f"{param}: время в виде мм:сс или ч:мм:сс, например 12:30")
    return minutes.seconds_list(found)


def get_transcript(
    meeting: str, from_time: str | None = None, to_time: str | None = None, speaker: str | None = None
) -> dict:
    """Lines "[mm:ss] Name: text" up to PIECE characters; a time range and one participant's lines, so as not to read
    too much. A long meeting comes in pieces: the next one starts at "следующий_from_time"."""
    found = _meeting(meeting)
    s, by = _seconds_of(from_time, "from_time", 0), _seconds_of(to_time, "to_time", float("inf"))
    speaker = privacy.restored(found.folder, speaker) if speaker else speaker  # a hidden name comes as its label
    lines = [
        (second, f"[{transcript.time(second)}] {text}")
        for second, text in _lines(found)
        if s <= second <= by and (speaker is None or text.startswith(f"{speaker}: "))
    ]
    piece, next_second = _piece(lines)
    answer = {
        **_card(found),
        "строк": len(piece),
        "текст": "\n".join(piece),
        "следующий_from_time": transcript.time(next_second) if next_second is not None else None,
    }
    return _hidden(found, answer)


def _piece(lines: list[tuple[float, str]]) -> tuple[list[str], float | None]:
    """Lines up to PIECE characters and the second the next piece starts at, None if all of them fit. The cut falls
    between whole seconds: the next piece, read from that second, repeats no line."""
    size = 0
    for number, (second, line) in enumerate(lines):
        size += len(line) + 1
        if size > PIECE and number:
            cut = number
            while cut > 1 and int(lines[cut - 1][0]) == int(second):
                cut -= 1
            return [z for _, z in lines[:cut]], lines[cut][0]
    return [z for _, z in lines], None


def _task(task: tasks.Task) -> dict:
    o_ = task.origin
    return {
        "ключ": task.key,
        "текст": task.text,
        "кто": task.who,
        "срок": task.due.isoformat() if task.due else None,
        "вид": task.kind,
        "состояние": task.status,
        "проект": task.project,
        "описание": task.description,
        "метки": list(task.labels),
        "приоритет": task.priority,
        "архив": task.archive,
        "чек_лист": [{"текст": p.text, "сделан": p.is_done} for p in task.checklist],
        "основание": {
            "встреча": o_.folder.name if o_.folder else None,
            "название": o_.title,
            "дата": o_.date.isoformat(),
            "время": transcript.time(o_.second) if o_.second is not None else None,
        },
    }


def list_open_items(
    project: str | None = None, date_from: str | None = None, date_to: str | None = None, include_done: bool = False
) -> dict:
    """Task board tasks and questions: from meeting minutes for the period and own ones added in the period; done
    and archived ones only with include_done."""
    s, by = _date(date_from, "date_from") or datetime.date.min, _date(date_to, "date_to") or datetime.date.max
    items = [
        _task(z)
        for z in tasks.tasks(settings.load().recordings, project)
        if (include_done or z.opened) and s <= z.origin.date <= by
    ]
    return {"пунктов": len(items), "пункты": items}


# Card edit fields: tool parameter to the board edit field
TASK_EDITS = {
    "text": "text",
    "description": "description",
    "assignee": "who",
    "due": "due",
    "priority": "priority",
    "labels": "labels",
    "status": "status",
}


def _editing(fields: dict) -> tasks.Edit:
    """update_task parameters as a board edit: due "" removes the due date, labels is the whole label list."""
    if foreign := sorted(fields.keys() - TASK_EDITS.keys()):
        raise ToolError(f"Нет параметров {', '.join(foreign)}. Есть: {', '.join(TASK_EDITS)}")
    if not fields:
        raise ToolError(f"Что поменять: хотя бы один из {', '.join(TASK_EDITS)}")
    edit_item = {TASK_EDITS[p]: z for p, z in fields.items() if z is not None}
    if "due" in edit_item:
        edit_item["due"] = _date(edit_item["due"], "due") or tasks.NO_DUE
    if "labels" in edit_item:
        edit_item["labels"] = tuple(edit_item["labels"])
    return tasks.Edit(**edit_item)


def update_task(task: str, **fields: object) -> dict:
    """Change only the passed card fields; for a minutes item the minutes themselves do not change."""
    edit_item = _editing(fields)
    recordings = settings.load().recordings
    if not any(z.key == task for z in tasks.tasks(recordings)):
        raise ToolError(f"Нет задачи «{task}». Ключи задач - в list_open_items, поле «ключ».")
    try:
        tasks.edit(recordings, task, edit_item)
    except ValueError as error:
        raise ToolError(str(error)) from None
    return next(_task(z) for z in tasks.tasks(recordings) if z.key == task)


def set_task_status(task: str, status: str) -> dict:
    return update_task(task, status=status)


def add_task(text: str, assignee: str = "", due: str | None = None, project: str = "") -> dict:
    if not text.strip():
        raise ToolError("text: что сделать")
    return _task(tasks.add(settings.load().recordings, text, project, tasks.Edit(who=assignee, due=_date(due, "due"))))


def get_project_notes(project: str) -> dict:
    """Project info and preparation for the next meeting: the analyst's questions and the collected agenda."""
    recordings = settings.load().recordings
    info = projects.notes(recordings, project)
    return {
        "проект": project,
        "заметки": info.text,
        "для_модели": info.for_model,
        "к_следующей_встрече": projects.for_meeting(recordings, project),
        "повестка": [p.text for p in tasks.agenda(recordings, project)],
    }


def get_meeting_notes(meeting: str) -> dict:
    """The analyst's notepad for the meeting: the agenda with "discussed" ticks and lines with the recording minute."""
    found = _meeting(meeting)
    notepad = notes.read(found.folder)
    return {
        **_card(found),
        "повестка": [{"пункт": p.text, "обсудили": p.discussed} for p in notepad.agenda],
        "строки": [
            {"время": transcript.time(s.second) if s.second is not None else None, "текст": s.text}
            for s in notepad.lines
            if s.text.strip()
        ],
    }


def get_costs(project: str | None = None, date_from: str | None = None, date_to: str | None = None) -> dict:
    """As in the summary: total, speech and minutes, by model and by meeting, in dollars; money counts on the day it
    was spent."""
    recordings = settings.load().recordings
    meetings = catalog.list_(recordings)
    expenses_ = [t_ for t_ in spending.list_(recordings, meetings) if project is None or t_.project == project]
    meetings = [v for v in meetings if project is None or v.project == project]
    s, by = _date(date_from, "date_from"), (_date(date_to, "date_to") or datetime.date(9998, 12, 31))
    by += datetime.timedelta(days=1)
    result = summary.result(meetings, s, by, expenses_)
    return {
        "всего": round(result.cost, 4),
        "речь": round(result.cost - result.minutes_list, 4),
        "протоколы": round(result.minutes_list, 4),
        "по_моделям": [
            {"вид": kind, "модель": name, "сколько": round(how_many, 4), "доля": round(share, 3)}
            for kind, name, how_many, share in summary.models(result)
        ],
        "по_встречам": [
            {
                "встреча": s_.folder.name if s_.folder else None,
                "название": s_.title,
                "дата": f"{s_.date:%Y-%m-%d}",
                "удалена": s_.folder is None,
                "речь": round(s_.result.cost - s_.result.minutes_list, 4),
                "протокол": round(s_.result.minutes_list, 4),
                "всего": round(s_.result.cost, 4),
            }
            for s_ in summary.by_meetings(meetings, s, by, expenses_)
        ],
    }


# --- edits ----------------------------------------------------------------------------------------------------


def _save_previous(folder: pathlib.Path) -> None:
    """Previous minutes texts in rotation, up to COPIES of them: two edits in a row do not lose the analyst's edit."""
    file = folder / minutes.FILE
    if not file.exists():
        return
    copies = [folder / PREVIOUS, *(folder / PREVIOUS.replace(".md", f".{n}.md") for n in range(2, COPIES + 1))]
    for newer, older in zip(reversed(copies[:-1]), reversed(copies[1:]), strict=True):
        if newer.exists():
            newer.replace(older)
    transcript.write_text(copies[0], file.read_text(encoding="utf-8"))


def _kind(kind: str | None) -> str | None:
    if kind is not None and kind not in [v.name for v in kinds.kinds()]:
        raise ToolError(f"kind: один из {', '.join(v.name for v in kinds.kinds())}")
    return kind


def _set_kind(found: catalog.Meeting, kind: str | None) -> str:
    """The meeting kind for the minutes; returns the previous one."""
    old_view = catalog.properties(found.folder).get("вид", "")
    if _kind(kind) is not None:
        catalog.set_(found.folder, kind=kind)
    return old_view


def _restore_kind(found: catalog.Meeting, kind: str | None, old_view: str) -> None:
    """The minutes are not written: the kind set for them goes back, unless the window set another one meanwhile."""
    if kind is None:
        return
    with transcript.locked(found.folder / catalog.PROPERTIES):
        if catalog.properties(found.folder).get("вид", "") == kind:
            catalog.set_(found.folder, kind=old_view)


def compose_protocol(meeting: str, kind: str | None = None) -> dict:
    found = _meeting(meeting)
    params = minutes.params(settings.load())
    old_view = _set_kind(found, kind)
    try:
        file = minutes.compose(found.folder, params, keep=_save_previous)
    except (providers.ProviderError, OSError) as error:  # FileNotFoundError: the meeting is not recognized
        _restore_kind(found, kind, old_view)
        raise ToolError(failures.text(error)) from error
    price = catalog.properties(found.folder).get("стоимость_протокола")
    return {**_card(found), "текст": file.read_text(encoding="utf-8"), "стоимость": price}


def get_protocol_task(meeting: str, kind: str | None = None) -> dict:
    """The task without the transcript and without writing anything: kind applies to this task only."""
    found = _meeting(meeting)
    try:
        job = minutes.job(found.folder, minutes.params(settings.load()), _kind(kind))
    except FileNotFoundError as error:
        raise ToolError(str(error)) from None
    card = _card(found)
    answer = {
        **card,
        "вид_встречи": kind or card["вид_встречи"],
        **job,
        "transcript": f"Расшифровка встречи ({len(_lines(found))} строк) не вложена: прочитайте ее целиком "
        "get_transcript кусками - первый вызов без from_time, следующие с from_time из «следующий_from_time» "
        "прошлого ответа, пока он не станет null. Не пропускайте куски: протокол - по всей встрече.",
        "how_to_save": "Составьте протокол строго по instruction из input и расшифровке и сохраните его "
        "save_protocol (meeting - это поле «встреча»). Ответ - только текст протокола в Markdown.",
    }
    return _hidden(found, answer, note="input")


def save_protocol(meeting: str, text: str, model: str = "") -> dict:
    if not text.strip():
        raise ToolError("text: текст протокола")
    found = _meeting(meeting)
    with transcript.locked(found.folder / minutes.FILE):  # minutes the window writes meanwhile go to the copies
        _save_previous(found.folder)
        minutes.save(found.folder, privacy.restored(found.folder, text), model.strip() or ASSISTANT)
    return _card(found)


def update_protocol(meeting: str, text: str) -> dict:
    if not text.strip():
        raise ToolError("text: новый текст протокола")
    found = _meeting(meeting)
    with transcript.locked(found.folder / minutes.FILE):
        _save_previous(found.folder)
        transcript.write_text(found.folder / minutes.FILE, privacy.restored(found.folder, text).rstrip() + "\n")
    return _card(found)


def set_speaker_names(meeting: str, names: dict[str, str]) -> dict:
    """Names instead of "Собеседник N"; "Я" is the owner, whose name is set in the program settings."""
    found = _meeting(meeting)
    participants = [who for who in transcript.participants(found.folder) if who != transcript.ME]
    if transcript.ME in names:
        raise ToolError("«Я» - это аналитик, его имя для протокола задается в настройках программы")
    if foreign := [who for who in names if who not in participants]:
        raise ToolError(f"Нет участников {', '.join(foreign)}. Есть: {', '.join(participants) or 'никого'}")
    transcript.set_names(found.folder, names)
    shares, speaker_names = transcript.shares(found.folder), transcript.names(found.folder)
    return {
        "участники": [
            {"кто": who, "имя": speaker_names.get(who, who), "доля": round(shares.get(who, 0), 2)}
            for who in transcript.participants(found.folder)
        ]
    }


def set_meeting_info(meeting: str, title: str | None = None, project: str | None = None) -> dict:
    found = _meeting(meeting)
    fields = {}
    if title is not None:
        fields["название"] = catalog.for_storage(found.folder, title)
    if project is not None:
        fields["проект"] = project.strip()
    if not fields:
        raise ToolError("Нужно title или project")
    catalog.set_(found.folder, **fields)
    return _card(_meeting(meeting))


# --- descriptions for the client ------------------------------------------------------------------------------

MEETING = {"meeting": {"type": "string", "description": "Имя папки встречи - поле «встреча» из list_meetings"}}
FILTER = {
    "project": {"type": "string", "description": "Только этот проект; пустая строка - встречи без проекта"},
    "date_from": {"type": "string", "description": "С этой даты включительно, ГГГГ-ММ-ДД"},
    "date_to": {"type": "string", "description": "По эту дату включительно, ГГГГ-ММ-ДД"},
}
LIMIT = {"limit": {"type": "integer", "minimum": 1, "description": "Сколько встреч вернуть"}}
TOOLS = {
    i.name: i
    for i in (
        Tool(
            "list_meetings",
            "Встречи аналитика, новые сверху: название, дата, длительность, проект, участники и "
            "итог протокола. Поле «встреча» - имя для остальных инструментов.",
            FILTER | LIMIT,
            list_meetings,
            read_only=True,
        ),
        Tool(
            "search_meetings",
            "Найти слово или фразу в расшифровках встреч: где нашлось и отрывки с временем.",
            {"query": {"type": "string", "description": "Что искать"}} | FILTER | LIMIT,
            search_meetings,
            ("query",),
            read_only=True,
        ),
        Tool(
            "get_protocol",
            "Протокол встречи. view: «полный» - черновик с метками времени и разделом «Для "
            "аналитика» (клиенту не отправлять), «письмо» - для письма клиенту, «база_знаний» - для базы знаний.",
            MEETING | {"view": {"type": "string", "enum": list(KINDS)}},
            get_protocol,
            ("meeting",),
            read_only=True,
        ),
        Tool(
            "get_transcript",
            "Расшифровка встречи строками «[мм:сс] Имя: текст», кусками до 30 тысяч знаков: если "
            "«следующий_from_time» не null, следующий кусок - с этим from_time. from_time и to_time - мм:сс или "
            "ч:мм:сс; speaker - реплики одного участника. Метки вида [ТЕЛЕФОН_1] - скрытые личные данные.",
            MEETING
            | {
                "from_time": {"type": "string"},
                "to_time": {"type": "string"},
                "speaker": {"type": "string", "description": "Имя участника, как в расшифровке"},
            },
            get_transcript,
            ("meeting",),
            read_only=True,
        ),
        Tool(
            "list_open_items",
            "Доска задач: задачи, замечания, открытые вопросы и решения на подтверждении из "
            "протоколов и задачи, добавленные вручную - что сделать, описание, кто, срок, вид (задача, вопрос, "
            "решение), состояние (сделать, в работе, готово), приоритет, метки, чек-лист и основание: встреча, "
            "дата и минута. Сделанные и убранные в архив - только с include_done. Для подготовки к следующей "
            "встрече с клиентом.",
            FILTER | {"include_done": {"type": "boolean"}},
            list_open_items,
            read_only=True,
        ),
        Tool(
            "set_task_status",
            "Перенести задачу доски в другую колонку: status - «сделать», «в работе» или "
            "«готово». task - поле «ключ» из list_open_items.",
            {"task": {"type": "string"}, "status": {"type": "string", "enum": list(tasks.STATES)}},
            set_task_status,
            ("task", "status"),
        ),
        Tool(
            "update_task",
            "Поправить карточку доски: меняются только переданные поля. text - что сделать, "
            "description - описание, assignee - кто, due - срок ГГГГ-ММ-ДД (пустая строка - убрать срок), "
            "priority - приоритет (пустая строка - без приоритета), labels - все метки карточки (новые "
            "заводятся сами), status - колонка. У пункта из протокола правка видна только на доске, сам протокол "
            "не меняется. task - поле «ключ» из list_open_items.",
            {
                "task": {"type": "string"},
                "text": {"type": "string"},
                "description": {"type": "string"},
                "assignee": {"type": "string"},
                "due": {"type": "string"},
                "priority": {"type": "string", "enum": list(tasks.PRIORITIES)},
                "labels": {"type": "array", "items": {"type": "string"}},
                "status": {"type": "string", "enum": list(tasks.STATES)},
            },
            update_task,
            ("task",),
            changes_data=True,
        ),
        Tool(
            "add_task",
            "Добавить свою задачу на доску (не из протокола): text - что сделать, assignee - кто, "
            "due - срок ГГГГ-ММ-ДД, project - проект.",
            {
                "text": {"type": "string"},
                "assignee": {"type": "string"},
                "due": {"type": "string"},
                "project": {"type": "string"},
            },
            add_task,
            ("text",),
        ),
        Tool(
            "get_project_notes",
            "Сведения аналитика о проекте (база, договоренности, термины, кто есть кто) и "
            "подготовка к следующей встрече: его вопросы и собранная повестка - вопросы, не обсужденное "
            "прошлый раз, открытые вопросы с доски. Для подготовки к встрече с клиентом.",
            {"project": {"type": "string"}},
            get_project_notes,
            ("project",),
            read_only=True,
        ),
        Tool(
            "get_meeting_notes",
            "Блокнот аналитика по встрече: повестка с отметками «обсудили» и его заметки "
            "с минутой записи. Заметки - то, что он счел главным.",
            MEETING,
            get_meeting_notes,
            ("meeting",),
            read_only=True,
        ),
        Tool(
            "get_costs",
            "Расходы на распознавание речи и протоколы в долларах: всего, по моделям и по встречам.",
            FILTER,
            get_costs,
            read_only=True,
        ),
        Tool(
            "compose_protocol",
            "Составить черновик протокола встречи языковой моделью по инструкции аналитика. "
            "kind - вид встречи, от него зависят разделы протокола; не задан - вид встречи из list_meetings "
            "(поле «вид_встречи»). Платно (около 0,15-0,2 $ за 40 минут встречи), 1-2 минуты. Прежний протокол "
            "сохраняется рядом. Только по просьбе пользователя.",
            MEETING
            | {
                "kind": {
                    "type": "string",
                    "enum": [v.name for v in kinds.kinds()],
                    "description": "; ".join(f"{v.name} - {v.description}" for v in kinds.kinds()),
                }
            },
            compose_protocol,
            ("meeting",),
            changes_data=True,
        ),
        Tool(
            "get_protocol_task",
            "Задание, чтобы составить протокол встречи своей моделью: instruction "
            "(инструкция аналитика) и input (сведения о встрече) - то же, что получает платный сервис программы; "
            "расшифровку прочитайте целиком get_transcript кусками. kind - вид встречи для этого задания, как у "
            "compose_protocol, не сохраняется. Потом - save_protocol. Только по просьбе пользователя.",
            MEETING | {"kind": {"type": "string", "enum": [v.name for v in kinds.kinds()]}},
            get_protocol_task,
            ("meeting",),
            read_only=True,
        ),
        Tool(
            "save_protocol",
            "Сохранить протокол, который вы составили по get_protocol_task (Markdown). "
            "model - ваша модель, для сведений. Прежний протокол сохраняется рядом.",
            MEETING
            | {
                "text": {"type": "string", "description": "Текст протокола", "maxLength": 200000},
                "model": {"type": "string"},
            },
            save_protocol,
            ("meeting", "text"),
            changes_data=True,
        ),
        Tool(
            "update_protocol",
            "Заменить текст протокола (Markdown в формате черновика). Прежний сохраняется "
            "рядом. Только после согласия пользователя.",
            MEETING | {"text": {"type": "string", "description": "Новый текст протокола", "maxLength": 200000}},
            update_protocol,
            ("meeting", "text"),
            changes_data=True,
        ),
        Tool(
            "set_speaker_names",
            "Вписать имена участников вместо «Собеседник N», например {«Собеседник 1»: "
            "«Анна»}. Расшифровка перепишется, распознавать заново не нужно.",
            MEETING | {"names": {"type": "object", "additionalProperties": {"type": "string"}}},
            set_speaker_names,
            ("meeting", "names"),
            changes_data=True,
        ),
        Tool(
            "set_meeting_info",
            "Поменять название встречи (title) или проект (project, пустая строка - без проекта).",
            MEETING | {"title": {"type": "string"}, "project": {"type": "string"}},
            set_meeting_info,
            ("meeting",),
            changes_data=True,
        ),
    )
}


def _request(meeting: str) -> str:
    return (
        f"Составь протокол встречи «{meeting}» из QG Knotes: найди ее в list_meetings, возьми задание "
        "get_protocol_task, составь протокол строго по инструкции из задания и сохрани его save_protocol."
    )


PROMPTS = {
    p.name: p
    for p in (
        Prompt(
            "compose_protocol",
            "Составить протокол встречи своей моделью",
            {"meeting": "Встреча: имя папки из list_meetings или название"},
            _request,
        ),
    )
}


def client_config() -> str:
    """Config for Claude Desktop and other MCP clients (the mcpServers section): how to start this program."""
    command, environment = system.startup("--mcp", console=True)
    server = {"command": command[0], "args": command[1:]} | ({"env": environment} if environment else {})
    return json.dumps({"mcpServers": {"qg-knotes": server}}, ensure_ascii=False, indent=2)


def main() -> int:
    """Runs as an MCP server until the input ends; the meetings app may be open at the same time."""
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    serve(TOOLS, INSTRUCTION, sys.stdin, sys.stdout, PROMPTS)
    return 0
