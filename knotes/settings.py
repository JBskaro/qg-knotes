# SPDX-License-Identifier: GPL-3.0-only
"""Settings: the file настройки.json in %APPDATA%\\QG Knotes, API keys in the Windows Credential Manager."""

import contextlib
import dataclasses
import json
import logging
import os
import pathlib
import types
import typing
import urllib.parse
import winreg

import keyring
import keyring.errors

from knotes import disk_names, language, providers, transcript

NAME = "QG Knotes"  # program name in files, folders and the Windows key store
SERVICE = NAME
MEETING_PROGRAMS = (  # (name in settings, name in the hint, processes); names are translated when shown
    ("Zoom", "Zoom", ("Zoom.exe",)),
    (language.tr_key("Яндекс Телемост"), language.tr_key("Телемост"), ("YandexTelemost.exe",)),
    ("Microsoft Teams", "Teams", ("MSTeams_8wekyb3d8bbwe", "Teams.exe")),
    (
        language.tr_key("Браузеры: Chrome, Edge, Яндекс, Firefox"),
        language.tr_key("браузер"),
        ("chrome.exe", "msedge.exe", "browser.exe", "firefox.exe"),
    ),
)
MEETING_APPS = tuple(process_ for *_, processes in MEETING_PROGRAMS for process_ in processes)
ALL_TIPS = ("мастер", "окно", "настройки")  # first-run setup and tips, each shown once
RETIRED_MINUTES_PRESETS = ("помощник",)  # "AI assistant via MCP" of 0.20: the assistant works next to any preset now
BROKEN = "настройки.испорчены.json"  # a corrupted settings file is set aside under this name
log = logging.getLogger(__name__)


def program_name(process_: str) -> str:
    """Name in the interface language: "YandexTelemost.exe" -> "Telemost"; an unknown program - its file name
    without .exe."""
    for _, name, processes in MEETING_PROGRAMS:
        if process_.lower() in (p.lower() for p in processes):
            return language.tr(name)
    return process_.removesuffix(".exe")


def data_folder(name: str = NAME) -> pathlib.Path:
    return pathlib.Path(os.environ.get("APPDATA") or pathlib.Path.home()) / name


def log_folder() -> pathlib.Path:
    return pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home()) / NAME


def models_folder() -> pathlib.Path:
    """Local speech recognition models: next to the log, no administrator rights needed, not in OneDrive."""
    return log_folder() / "модели"


@dataclasses.dataclass
class Settings:
    provider: str = providers.LOCAL  # speech recognition preset
    model: str = ""  # empty - the preset's model
    local_model: str = ""  # local speech recognition: model id; empty - the preset's model
    address: str = ""  # empty - the preset's address
    language: str = "ru"
    recordings_folder: str = str(pathlib.Path.home() / "Documents" / NAME)  # not in the program folder or the repo
    vocabulary: list[dict] = dataclasses.field(default_factory=list)  # [{"правильно": ..., "варианты": [...]}]
    after_recording: str = "спросить"  # "спросить", "сразу" or "нет": whether to send for transcription
    suggest_recording: bool = True
    meeting_apps: list[str] = dataclasses.field(default_factory=lambda: list(MEETING_APPS))
    provider_retention: bool = False
    speaker_diarization: bool = True  # local: tell the other participants apart
    live_transcript: bool = False  # local: recognize during the recording too, see live.py
    local_gpu: bool = True  # use GPU acceleration for supported speech models
    models_mirror: str = ""  # address instead of huggingface.co for model downloads, e.g. https://hf-mirror.com
    record_screen: bool = False  # record the meeting screen to видео.mp4 along with the sound
    screen_capture: str = (
        ""  # what to capture: empty - the meeting window, "экран_встречи", "основной" or a device \\.\DISPLAY2
    )
    video_quality: str = "обычное"  # "эконом", "обычное" or "высокое"
    delete_audio_after: int = -1  # days after transcription; -1 - never, 0 - at once
    theme: str = "система"  # "система", "светлая" or "темная"
    ui_language: str = ""  # "ru" or "en"; empty - the Windows language
    open_in: str = "программе"  # transcript: "программе" (own window), "блокноте", "по_умолчанию" (as in Windows)
    hotkey: str = "Ctrl+Alt+R"  # start and stop recording; empty - off
    mark_hotkey: str = "Ctrl+Alt+M"  # "★ Mark" during recording
    notes_hotkey: str = "Ctrl+Alt+N"  # note field during recording
    hide_bar: bool = True  # the recording bar is kept out of screen sharing
    bar_collapsed: bool = False
    bar_position: list[int] | None = None
    notepad_place: list[int] | None = None  # x, y, width, height of the notepad during recording
    projects: list[str] = dataclasses.field(default_factory=list)  # created projects, including ones with no meetings
    project_looks: dict[str, dict] = dataclasses.field(default_factory=dict)  # project -> {"цвет": 0-6, "значок"}
    window_layout: dict = dataclasses.field(default_factory=dict)  # meetings window: hidden columns and widths
    microphone: str = ""  # device name; empty - as selected in Windows
    output: str = ""  # headphones or speakers that play the other participants; empty - as in Windows
    minutes_service: str = "openrouter"  # minutes preset
    minutes_model_id: str = ""  # empty - the preset's model
    minutes_address: str = ""  # empty - the preset's address
    my_name: str = ""  # replaces the speaker "Я" (me) in the minutes
    hide_personal_data: bool = True  # phones, e-mails, INN and the like go to a cloud minutes model as labels
    hide_names: bool = False  # people's names too: the model cannot write full names and assignees then
    tips_shown: list[str] = dataclasses.field(default_factory=list)  # from ALL_TIPS
    check_for_updates: bool = True  # once a day ask GitHub whether a newer version is out
    updates_checked: str = ""  # ISO date of the last successful check

    @property
    def recordings(self) -> pathlib.Path:
        return pathlib.Path(self.recordings_folder)


def settings_file() -> pathlib.Path:
    return data_folder() / "настройки.json"


def read(path: pathlib.Path | None = None, set_aside: bool = True) -> tuple[Settings, str]:
    """Settings and what is wrong with them (empty - all fine). A corrupted file is set aside next to it and the
    program starts with default settings: otherwise it would not start at all. A field of the wrong type takes its
    default, the file is copied aside and repaired. set_aside=False (the MCP server reads the settings on every
    call) leaves the file as it is; otherwise the reading and the repair are one edit under the lock of the folder."""
    path = path or settings_file()
    if not path.exists():
        return Settings(), ""
    with transcript.locked(path) if set_aside else contextlib.nullcontext():
        return _read(path, set_aside)


def _read(path: pathlib.Path, set_aside: bool) -> tuple[Settings, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        data = None
    if not isinstance(data, dict):
        log.warning("corrupted settings file %s, starting with default settings", path)
        return Settings(), _set_aside(path, set_aside, move=True)
    types_ = {f.name: f.type for f in dataclasses.fields(Settings)}
    given = {k: z for k, z in disk_names.from_disk(data).items() if k in types_}
    wrong = sorted(k for k, z in given.items() if not _fits(z, types_[k]))
    config = Settings(**{k: z for k, z in given.items() if k not in wrong})
    problem = ""
    if wrong:
        log.warning("settings fields of the wrong type take their defaults: %s", ", ".join(wrong))
        problem = _set_aside(path, set_aside, move=False)
    if config.minutes_service in RETIRED_MINUTES_PRESETS:
        config.minutes_service = Settings.minutes_service
    if config.provider == providers.LOCAL and config.model and not config.local_model:
        config.local_model, config.model = config.model, ""  # old settings kept the local model in model
    if "подсказки_показаны" not in data:  # settings from before the tips: the program is already set up
        config.tips_shown = list(ALL_TIPS)
    if wrong and set_aside:
        _repair(config, path)
    return config, problem


def _fits(value: object, kind: object) -> bool:
    """The value has the field's type: str, bool, int (not bool), a list or dict of such, None where allowed."""
    if isinstance(kind, types.UnionType):
        return any(_fits(value, k) for k in typing.get_args(kind))
    origin, args = typing.get_origin(kind) or kind, typing.get_args(kind)
    if not isinstance(value, origin) or (origin is int and isinstance(value, bool)):
        return False
    if isinstance(value, list) and args:
        return all(_fits(z, args[0]) for z in value)
    if isinstance(value, dict) and args:
        return all(_fits(k, args[0]) and _fits(z, args[1]) for k, z in value.items())
    return True


def _set_aside(path: pathlib.Path, set_aside: bool, move: bool) -> str:
    """The corrupted file next to it as BROKEN: moved when it cannot be read at all, copied when only some fields
    are wrong; the message for the user."""
    if not set_aside:
        return _corrupted(path)
    if not move:
        copy_ = transcript.set_aside(path, path.with_name(BROKEN))
    else:
        try:
            copy_ = path.replace(path.with_name(BROKEN))
        except OSError as error:
            log.warning("cannot set aside the corrupted settings file %s: %s", path, error)
            copy_ = None
    if copy_ is None:
        return language.tr(
            "The settings file was corrupted, so the app started with default settings. Couldn't save the "
            "corrupted file aside: {path}.",
            path=path,
        )
    return _corrupted(copy_)


def _corrupted(copy_: pathlib.Path) -> str:
    return language.tr(
        "The settings file was corrupted, so the app started with default settings. The corrupted file is saved "
        "next to it: {copy_}.",
        copy_=copy_,
    )


def _repair(config: Settings, path: pathlib.Path) -> None:
    """Wrong fields are written with their defaults: the message about them comes once."""
    try:
        save(config, path)
    except OSError as error:
        log.warning("cannot repair the settings file %s: %s", path, error)


def load(path: pathlib.Path | None = None) -> Settings:
    """Settings without touching the file: for the MCP server and the command line, which read them on every call."""
    return read(path, set_aside=False)[0]


def ui_language(path: pathlib.Path | None = None) -> str:
    """Interface language before the windows start, read only: a corrupted file is handled and set aside by read(),
    with a message to the user."""
    try:
        data = json.loads((path or settings_file()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return data.get("язык_интерфейса", "") if isinstance(data, dict) else ""


def save(config: Settings, path: pathlib.Path | None = None) -> None:
    """Under the folder lock: a save waits for a repair of the file in progress instead of being written over by it."""
    path = path or settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with transcript.locked(path):
        transcript.write_json(path, disk_names.to_disk(dataclasses.asdict(config)))


def _user_variable(name: str) -> str:
    """Environment variable, including one set with setx after the process started."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as registry:
            return str(winreg.QueryValueEx(registry, name)[0])
    except OSError:
        return ""


def key(name: str, variable: str = "") -> str:
    """API key from the Windows key store, otherwise from the environment variable."""
    value = keyring.get_password(SERVICE, name)
    return value or (_user_variable(variable) if variable else "")


def set_key(name: str, value: str) -> None:
    if value:
        keyring.set_password(SERVICE, name, value)
        return
    with contextlib.suppress(keyring.errors.PasswordDeleteError):  # there was no key
        keyring.delete_password(SERVICE, name)


def _key_name(address: str) -> str:
    """The key is stored under the address's server: speech recognition and minutes on one server share it."""
    return urllib.parse.urlsplit(address.strip()).netloc.lower()


def address_key(address: str, previous: str = "", variable: str = "") -> str:
    """Server key. The old key name (not bound to an address) and the key variable belong to the preset's server:
    only an address on it gets them, and the old key moves under the address. A preset address changed to another
    server (a reseller) never gets the preset's key."""
    name = _key_name(address)
    if not name:
        return ""
    own = _preset_server(name, previous, variable)
    if (
        own
        and previous
        and not keyring.get_password(SERVICE, name)
        and (stale := keyring.get_password(SERVICE, previous))
    ):
        keyring.set_password(SERVICE, name, stale)
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(SERVICE, previous)
        log.info("the key %s moved under the server %s", previous, name)
    return key(name, variable if own else "")


def _preset_server(name: str, previous: str, variable: str) -> bool:
    """The server is the one of the preset owning the old key name or the variable; the own server preset has no
    fixed server, its old key was entered for whatever server the user set."""
    owners = [
        p
        for function in providers.FUNCTIONS
        for p in providers.list_(function)
        if (previous and p.key == previous) or (variable and p.variable == variable)
    ]
    return any(not p.key_needed or _key_name(p.address) == name for p in owners)


def set_address_key(address: str, value: str) -> None:
    if name := _key_name(address):
        set_key(name, value)
