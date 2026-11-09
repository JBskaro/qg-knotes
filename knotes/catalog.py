# SPDX-License-Identifier: GPL-3.0-only
"""Meeting catalog: folders and files in the recordings folder, their status, title and project, search, price,
audio cleanup."""

import dataclasses
import contextlib
import datetime
import functools
import logging
import os
import pathlib
import time

from knotes import disk_names, language, screen, transcript

log = logging.getLogger(__name__)
ERROR = "ошибка.txt"
PROPERTIES = "встреча.json"  # custom title, project, recording duration
INTERRUPTED, RECOGNITION_ERROR, READY, RECORDED, FILE = "прервана", "ошибка", "готова", "записана", "файл"
RECORDING_FILES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4a", ".mp3", ".wav", ".ogg", ".flac", ".aac", ".wma"}
VIDEO = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
TODAY, YESTERDAY = language.tr_key("Сегодня"), language.tr_key("Вчера")  # meeting list groups, shown via language.tr()
THIS_WEEK, EARLIER = language.tr_key("На этой неделе"), language.tr_key("Раньше")
PARTICIPANTS = language.forms("участник", "участника", "участников")
SPEECH, MINUTES_KIND = (
    language.tr_key("Речь"),
    language.tr_key("Протоколы"),
)  # what a meeting's money goes to: speech recognition and minutes
NO_PROJECT = language.tr_key("Без проекта")  # name for meetings without a project in filters and breakdowns
MEETING = language.tr_key("Встреча {date}")  # default title: Russian in data and for the model, shown via language.tr()
FOLDER_NAME = "%Y-%m-%d_%H-%M"  # a meeting folder is named by its start time: "2026-10-22_10-18"
_MEETING_FILES = frozenset(
    (
        transcript.MANIFEST,
        ERROR,
        PROPERTIES,
        transcript.TRANSCRIPT,
        transcript.PHRASES,
        transcript.SOURCE,
        *(f"{d}.flac" for d in transcript.TRACKS),
    )
)


@dataclasses.dataclass
class Meeting:
    folder: pathlib.Path  # for a ready-made file not yet taken into work, the file itself
    status: str
    seconds: float = 0.0
    cost: float | None = None
    participant_count: int = 0
    error: str = ""
    custom_title: str = ""
    project: str = ""
    minutes_cost: float = 0.0  # already included in the meeting cost
    model: str = ""  # speech recognition model
    minutes_model: str = ""
    started: datetime.datetime | None = dataclasses.field(default=None, compare=False)  # set once by meeting()

    @property
    def title(self) -> str:
        return self.folder.name

    @property
    def expenses(self) -> list[tuple[str, str, float]]:
        """What the money went to: (speech or minutes, model, amount)."""
        minutes = self.minutes_cost
        result = [(SPEECH, self.model, (self.cost or 0) - minutes), (MINUTES_KIND, self.minutes_model, minutes)]
        return [r for r in result if r[2] > 0]

    @property
    def heading(self) -> str:
        return self.custom_title or default_title(self.folder)

    @property
    def display_title(self) -> str:
        """Title in the interface language: a custom one as is, the default one like "Meeting 22.10 10:18"."""
        return self.custom_title or default_title(self.folder, view_state=True)

    @property
    def date(self) -> datetime.datetime:
        """Start time; for a meeting from a ready-made file it is read from the disk once."""
        if self.started is None:
            self.started = folder_date(self.folder)
        return self.started

    @property
    def has_audio(self) -> bool:
        return self.status == FILE or any((self.folder / f"{d}.flac").exists() for d in transcript.TRACKS)

    @property
    def transcript(self) -> pathlib.Path:
        return self.folder / transcript.TRANSCRIPT


def folder_date(folder: pathlib.Path, given: dict | None = None) -> datetime.datetime:
    """Start time from a name like "2026-10-22_10-18"; for a ready-made file and a meeting made from it, when the file
    was written (not when it was taken into work), and if the file is gone, when the folder appeared. A meeting from a
    file keeps that date in its properties (given, if already read), so the file is not looked for again."""
    if (date := name_date(folder.name)) is not None:
        return date
    if stored := (properties(folder) if given is None else given).get("начало"):
        with contextlib.suppress(TypeError, ValueError):
            return datetime.datetime.fromisoformat(stored)
    source = folder / transcript.SOURCE
    files = [folder] if folder.is_file() else []
    files += [pathlib.Path(source.read_text(encoding="utf-8").strip())] if source.exists() else []
    files += [folder.with_name(folder.name + extension) for extension in sorted(RECORDING_FILES)]
    file = next((f for f in files if f.is_file()), None)
    return datetime.datetime.fromtimestamp(file.stat().st_mtime if file else folder.stat().st_birthtime)


@functools.lru_cache(maxsize=4096)
def name_date(name: str) -> datetime.datetime | None:
    """Start time from a folder name like "2026-10-22_10-18"; None for another name."""
    try:
        return datetime.datetime.strptime(name[:16], FOLDER_NAME)
    except ValueError:
        return None


def default_title(folder: pathlib.Path, today: datetime.date | None = None, view_state: bool = False) -> str:
    """Turns "2026-10-22_10-18" into MEETING with "22.10 10:18" (the year only if not the current one); a ready-made
    file keeps its name. view_state: in the interface language, for the screen."""
    date = name_date(folder.name)
    if date is None:
        return folder.name
    kind = "%d.%m %H:%M" if date.year == (today or datetime.date.today()).year else "%d.%m.%Y %H:%M"
    return language.tr(MEETING, date=date.strftime(kind)) if view_state else MEETING.format(date=date.strftime(kind))


def heading(folder: pathlib.Path) -> str:
    return properties(folder).get("название") or default_title(folder)


def display_title(folder: pathlib.Path) -> str:
    return properties(folder).get("название") or default_title(folder, view_state=True)


def for_storage(folder: pathlib.Path, title: str) -> str:
    """Title from the input field for the meeting properties: the default title, in any language, is not stored."""
    title = title.strip()
    return "" if title in (default_title(folder), default_title(folder, view_state=True)) else title


def properties(folder: pathlib.Path) -> dict:
    return transcript.read_json(folder / PROPERTIES, {}) if folder.is_dir() else {}


def set_(folder: pathlib.Path, **fields: object) -> None:
    """Meeting properties over the saved ones, under the meeting folder lock: the window and the MCP server setting
    different properties at once keep both. An empty value removes the property (the title goes back to the
    default)."""
    new_items = disk_names.to_disk(fields)
    transcript.update_json(
        folder / PROPERTIES, {}, lambda saved: {k: z for k, z in (saved | new_items).items() if z not in ("", None)}
    )


def _state(folder: pathlib.Path) -> str:
    if (folder / transcript.MANIFEST).exists():
        return INTERRUPTED
    if (folder / ERROR).exists():
        return RECOGNITION_ERROR
    if (folder / transcript.TRANSCRIPT).exists():
        return READY
    if any((folder / f"{d}.flac").exists() for d in transcript.TRACKS) or (folder / transcript.SOURCE).exists():
        return RECORDED
    return ""


def _ready_meeting(result: Meeting) -> None:
    data = transcript.phrases(result.folder)
    if data is None:
        return
    result.seconds = float(data["сведения"].get("длительность", 0))
    result.cost = data["сведения"].get("стоимость")
    result.model = data["сведения"].get("провайдер", "").rpartition(", ")[2]  # "OpenRouter, elevenlabs/scribe-v2"
    result.participant_count = len({f["кто"] for f in data["фразы"]})


def meeting(folder: pathlib.Path) -> Meeting | None:
    status = _state(folder)
    if not status:
        return None
    given = properties(folder)
    result = Meeting(
        folder,
        status,
        seconds=float(given.get("секунд", 0)),
        custom_title=given.get("название", ""),
        project=given.get("проект", ""),
        started=folder_date(folder, given),
    )
    if status == READY:
        _ready_meeting(result)
        result.minutes_cost = float(given.get("стоимость_протокола") or 0)
        result.minutes_model = given.get("модель_протокола", "")
        if result.minutes_cost:
            result.cost = (result.cost or 0) + result.minutes_cost
    if status == RECOGNITION_ERROR:
        result.error = (folder / ERROR).read_text(encoding="utf-8")
    return result


def _row_label(folder: pathlib.Path) -> tuple:
    """Name, mtime and size of the files a list entry depends on: one directory listing, no file is opened."""
    with os.scandir(folder) as elements:
        return tuple(
            sorted((e_.name, e_.stat().st_mtime_ns, e_.stat().st_size) for e_ in elements if e_.name in _MEETING_FILES)
        )


@functools.lru_cache(maxsize=4096)
def _read_cached(folder: pathlib.Path, _files_key: tuple) -> Meeting | None:
    """A meeting is re-read only when its files changed: the window refreshes the whole list every few seconds."""
    return _meeting_or_none(folder)


def _meeting_or_none(folder: pathlib.Path) -> Meeting | None:
    """Skip a folder with corrupted data (edited by hand, someone else's meeting) so the other meetings stay
    visible."""
    try:
        return meeting(folder)
    except (ValueError, TypeError, KeyError, AttributeError):
        log.warning("skipped a meeting with corrupted data: %s", folder.name, exc_info=True)
        return None


def list_(recordings: pathlib.Path) -> list[Meeting]:
    """Meetings from newest to oldest by start time, plus ready-made recordings (video, audio) put into the recordings
    folder but not yet taken into work. Empty and foreign folders are skipped."""
    if not recordings.exists():
        return []
    elements = list(recordings.iterdir())
    folders = {p.name for p in elements if p.is_dir()}
    result = []
    for element in elements:
        if element.is_dir():
            try:
                read_item = _read_cached(element, _row_label(element))
            except OSError as error:  # deleted while listing, or the network drive dropped
                log.warning("skipped meeting folder %s: %s", element.name, error)
                continue
            result.append(read_item and dataclasses.replace(read_item))  # a copy: edits keep the cached one intact
        elif element.suffix.lower() in RECORDING_FILES and element.stem not in folders:
            result.append(Meeting(element, FILE, started=folder_date(element)))
    return sorted((v for v in result if v), key=lambda v: v.date, reverse=True)


def projects(meetings: list[Meeting], known: list[str] | None = None) -> list[str]:
    """Projects of the meetings plus those created in advance (with no meetings yet), alphabetically."""
    return sorted({v.project for v in meetings if v.project} | set(known or []), key=str.lower)


def account_by_projects(meetings: list[Meeting]) -> dict[str, int]:
    """How many meetings each project has; the key "" is meetings without a project."""
    account: dict[str, int] = {}
    for v in meetings:
        account[v.project] = account.get(v.project, 0) + 1
    return account


def to_project(folders: list[pathlib.Path], project: str) -> None:
    """Move meetings into a project; an empty project takes them out of their project. Ready-made files without a
    folder are skipped."""
    for folder in folders:
        if folder.is_dir():
            set_(folder, project=project)


def rename_project(meetings: list[Meeting], old_value: str, new_value: str) -> int:
    """Sets the new project name on all its meetings (empty leaves them without a project); returns how many meetings
    changed."""
    folders = [v.folder for v in meetings if v.project == old_value and v.folder.is_dir()]
    to_project(folders, new_value)
    return len(folders)


def day(date: datetime.date, today: datetime.date | None = None) -> str:
    """Group in the meeting list: TODAY, YESTERDAY, THIS_WEEK or EARLIER."""
    today = today or datetime.date.today()
    if date == today:
        return TODAY
    if date == today - datetime.timedelta(days=1):
        return YESTERDAY
    return THIS_WEEK if date.isocalendar()[:2] == today.isocalendar()[:2] and date < today else EARLIER


def duration(seconds: float) -> str:
    minutes_count = max(1, round(seconds / 60))
    if minutes_count >= 60:
        return language.tr(
            "{hours} h {minutes_count} min", hours=minutes_count // 60, minutes_count=f"{minutes_count % 60:02d}"
        )
    return language.tr("{minutes_count} min", minutes_count=minutes_count)


def money(amount: float) -> str:
    if amount < 0.01:
        return language.tr("less than $0.01")
    return f"{amount:.2f} $".replace(".", ",") if language.current() == "ru" else f"${amount:.2f}"


def breakdown(total: float, minutes_list: float) -> str:
    """What the price is made of: "speech 0,28 $, minutes 0,18 $"; empty without minutes."""
    if not minutes_list:
        return ""
    return language.tr(
        "speech {speech}, minutes {minutes_list}", speech=money(total - minutes_list), minutes_list=money(minutes_list)
    )


def summary(meeting_: Meeting) -> str:
    """Line under the title: "10:00 · 38 min · 3 participants"; for a file, "video" or "audio". Prices are in the
    summary."""
    if meeting_.status == FILE:
        return language.tr("video") if meeting_.folder.suffix.lower() in VIDEO else language.tr("audio")
    parts = [meeting_.date.strftime("%H:%M")]
    if meeting_.seconds:
        parts.append(duration(meeting_.seconds))
    if meeting_.participant_count:
        parts.append(language.count(meeting_.participant_count, PARTICIPANTS))
    if meeting_.status == RECORDED:
        parts.append(language.tr("recording"))
    return " · ".join(parts)


def for_search(text: str) -> str:
    """Text to compare in search: case-insensitive, "ё" as "е"."""
    return text.lower().replace("ё", "е")


def find(meetings: list[Meeting], text: str) -> dict[pathlib.Path, int]:
    """How many times the text occurs in the title and transcript of each meeting (case-insensitive)."""
    query = for_search(text.strip())
    result = {}
    for v in meetings:
        count = for_search(" ".join((v.title, v.display_title, v.project))).count(query)  # what people see
        if v.status == READY:
            count += _found_in_file(v.transcript, query)
        if count:
            result[v.folder] = count
    return result


def _found_in_file(path: pathlib.Path, query: str) -> int:
    """How many times the query occurs in the transcript; the window repeats the same search every few seconds, the
    file is read again only when it changed or the query is new."""
    try:
        stat = path.stat()
        return _count_in_file(path, stat.st_mtime_ns, stat.st_size, query)
    except OSError as error:
        log.warning("cannot read %s for search: %s", path, error)
        return 0


@functools.lru_cache(maxsize=8192)
def _count_in_file(path: pathlib.Path, _mtime_ns: int, _size: int, query: str) -> int:
    return for_search(path.read_text(encoding="utf-8")).count(query)


def price_per_second(meetings: list[Meeting]) -> float | None:
    """Average price of a second of audio over past recognitions (from provider answers); None if there is no
    data."""
    seconds = cost = 0.0
    for v in meetings:
        if v.status == FILE:
            continue
        answers = v.folder / transcript.ANSWERS
        try:
            stat = answers.stat()
        except OSError:  # no answers: recognized on this computer or not yet
            continue
        meeting_seconds, meeting_cost = _usage(answers, stat.st_mtime_ns, stat.st_size)
        seconds, cost = seconds + meeting_seconds, cost + meeting_cost
    return cost / seconds if seconds and cost else None


@functools.lru_cache(maxsize=4096)
def _usage(answers: pathlib.Path, _mtime_ns: int, _size: int) -> tuple[float, float]:
    """Seconds and cost of a meeting's recognition answers, read again only when the file changed: it is up to 4 MB
    per hour of a meeting. A broken answer is skipped, the others count."""
    seconds = cost = 0.0
    for track, parts in transcript.read_json(answers, {}).items():
        for part in parts if isinstance(parts, list) else [parts]:
            try:
                usage = (part.get("ответ") or {}).get("usage") or {}
                part_seconds, part_cost = float(usage.get("seconds") or 0), float(usage.get("cost") or 0)
            except (AttributeError, TypeError, ValueError):
                log.warning("skipped a broken answer of %s in %s", track, answers)
                continue
            seconds, cost = seconds + part_seconds, cost + part_cost
    return seconds, cost


def clear_audio(meetings: list[Meeting], days_count: int, now: float | None = None) -> list[pathlib.Path]:
    """Deletes the tracks and video of recognized meetings whose transcript is older than days_count days; the
    transcript and answers stay."""
    boundary = (now or time.time()) - days_count * 86400
    cleaned = []
    for v in meetings:
        video = v.folder / screen.VIDEO_FILE
        if v.status == READY and (v.has_audio or video.exists()) and v.transcript.stat().st_mtime < boundary:
            for track in transcript.TRACKS:
                (v.folder / f"{track}.flac").unlink(missing_ok=True)
            video.unlink(missing_ok=True)
            cleaned.append(v.folder)
    return cleaned


def mark_error(folder: pathlib.Path, text: str) -> None:
    transcript.write_text(folder / ERROR, text)


def clear_error(folder: pathlib.Path) -> None:
    (folder / ERROR).unlink(missing_ok=True)
