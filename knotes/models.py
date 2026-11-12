# SPDX-License-Identifier: GPL-3.0-only
"""Local speech recognition models: the models.json catalog, custom models from Hugging Face (свои.json), state on
disk, download via curl. Each model has a folder <id> with files and the модель.json marker; the marker is written
last: without it the model is not downloaded, <name>.part files wait to be resumed.
"""

import collections
import contextlib
import dataclasses
import errno
import functools
import hashlib
import json
import logging
import pathlib
import re
import shutil
import sys
import tarfile
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable

from knotes import disk_names, providers, transcript
from knotes.language import tr_key, tr

CATALOG = pathlib.Path(__file__).with_name("models.json")
OWN_FILE = "свои.json"
MARKER = "модель.json"
PART = ".part"
HF = "https://huggingface.co"
HF_AUTHOR = "csukuangfj"  # publishes sherpa-onnx models on Hugging Face
SITE = f"/api/models?author={HF_AUTHOR}&search=sherpa-onnx&limit=1000&expand[]=siblings&expand[]=sha"
PAUSES = (3, 10, 30, 30)  # seconds between five attempts on one address
REPLACE_TRIES = 10  # os.replace attempts while an antivirus holds the new file
MARGIN = 1.1  # disk space needed is 10% more than the file size
# Only files without code: .bin, .pt, .pth, .ckpt and .pkl (PyTorch, pickle) run code when opened
SAFE_SUFFIXES = (".onnx", ".txt", ".json", ".yaml")
DEVICES = r"(?!(?:con|prn|aux|nul|com\d|lpt\d)(?:\.|$))"  # Windows device names: no file can have them
# A name without folders, Latin letters only: curl for Windows may not understand Cyrillic
FILE_NAME_PATTERN = re.compile(rf"{DEVICES}\w[\w.+-]{{0,127}}(?<!\.)", re.ASCII | re.IGNORECASE)
ID_PATTERN = re.compile(rf"{DEVICES}[a-z0-9][a-z0-9._-]{{0,79}}(?<!\.)")
HF_NAME = r"[A-Za-z0-9][\w.-]{0,95}"
REPOSITORY = re.compile(rf"{HF_NAME}/{HF_NAME}", re.ASCII)
HF_HOSTS = ("huggingface.co", "www.huggingface.co", "hf.co", "hf-mirror.com")
SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
ROLES = {
    "tokens": re.compile(r"(?:.*[-_.])?tokens\.txt"),
    "encoder": re.compile(r".*encoder.*\.onnx"),
    "decoder": re.compile(r".*decoder.*\.onnx"),
    "joiner": re.compile(r".*joiner.*\.onnx"),
    "model": re.compile(r"model.*\.onnx"),
}
LANGUAGES = {
    "ru": tr_key("Русский"),
    "en": tr_key("Английский"),
    "zh": tr_key("Китайский"),
    "de": tr_key("Немецкий"),
    "fr": tr_key("Французский"),
    "es": tr_key("Испанский"),
    "ja": tr_key("Японский"),
    "ko": tr_key("Корейский"),
    "yue": tr_key("Кантонский"),
    "vi": tr_key("Вьетнамский"),
    "pt": tr_key("Португальский"),
    "be": tr_key("Белорусский"),
    "hr": tr_key("Хорватский"),
    "it": tr_key("Итальянский"),
    "pl": tr_key("Польский"),
    "uk": tr_key("Украинский"),
}
LANGUAGE_WORDS = {
    "russian": "ru",
    "giga": "ru",
    "english": "en",
    "distil": "en",
    "cantonese": "yue",
    "aishell": "zh",
    "multi": "*",
    "multilingual": "*",
    "languages": "*",
}
TITLE_WORDS = {
    "whisper": "Whisper",
    "zipformer": "Zipformer",
    "paraformer": "Paraformer",
    "parakeet": "Parakeet",
    "conformer": "Conformer",
    "fastconformer": "FastConformer",
    "citrinet": "Citrinet",
    "lstm": "LSTM",
    "ctc": "CTC",
    "tdt": "TDT",
    "transducer": "Transducer",
}
EXTRA_WORDS = {
    "sherpa",
    "onnx",
    "nemo",
    "int8",
    "fp16",
    "stt",
    "russian",
    "english",
    "cantonese",
    "multi",
    "lang",
    "languages",
    "bilingual",
    "trilingual",
    "punct",
    "case",
    "pc",
}
# Adds punctuation: "punct" and "pc" in the name, Whisper, SenseVoice, the large Parakeet
PUNCTUATION = re.compile(r"punct|[-_]pc(?:[-_]|$)|whisper|sense-?voice|parakeet-tdt-0\.6b", re.IGNORECASE)
ADDRESS_END_CODES = (401, 403, 404, 410)  # retrying this address is useless
FINAL_EXIT_CODES = (1, 60, 63)  # curl: the network blocks, the certificate failed, the file is too big
NO_HOST = (6, 7)  # address not found, connection refused: a retry will not help
NOT_RESPONDING = (28, 35)  # the host did not answer in time, the secure connection failed: one retry
DOWNLOADED, PARTIAL, BROKEN, NONE = "скачана", "частично", "повреждена", "нет"

BROKEN_TEXT = tr_key("Файл пришел поврежденным. Повторите позже.")
WEB_PAGE = tr_key(
    "Вместо файла пришла веб-страница: возможно, сеть требует входа (Wi-Fi в гостинице, сеть "
    "организации). Откройте любой сайт в браузере, войдите и повторите."
)
MISSING_OR_PRIVATE = tr_key("Модель {repository} не найдена на Hugging Face или закрыта. Проверьте ссылку.")
CLOSED = tr_key(
    "Модель {repository} закрыта: Hugging Face отдает ее только после входа и согласия с условиями. "
    "Такие модели программа не скачивает."
)
NOT_A_LINK = tr_key(
    "Это не ссылка на модель Hugging Face. Нужна ссылка вида https://huggingface.co/владелец/модель "
    "или имя владелец/модель."
)
NO_ACCESS = tr_key("Нет доступа к папке моделей {folder}. Проверьте права или антивирус и повторите.")
NO_SIZE = tr_key("У модели «{title}» не известен размер файлов: Hugging Face не отдал сведения о ней. Повторите позже.")
UNCLEAR_ANSWER = tr_key("непонятный ответ, возможно, сеть требует входа.")
CURL_REASONS = {
    1: tr_key(
        "Сеть не пустила к {host}: возможно, она требует входа (Wi-Fi в гостинице, сеть организации). Откройте "
        "любой сайт в браузере, войдите и повторите."
    ),
    16: tr_key("Загрузка с {host} оборвалась. Нажмите «Скачать» еще раз: докачается с того же места."),
    28: tr_key("{host} не отвечает или отдает файл слишком медленно."),
    35: tr_key(
        "Не удалось установить защищенное соединение с {host}. Так бывает в сети организации и с антивирусом, "
        "который проверяет соединения: попробуйте другую сеть или укажите зеркало."
    ),
    60: tr_key(
        "Сертификат {host} не прошел проверку: сеть может подменять соединение (прокси, антивирус). Попробуйте "
        "другую сеть или укажите зеркало."
    ),
    63: tr_key("С {host} пришел файл больше ожидаемого: это не тот файл. Повторите позже."),
}
CURL_REASONS[18] = CURL_REASONS[92] = CURL_REASONS[16]
log = logging.getLogger(__name__)
_LOCKS: dict[str, threading.Lock] = {}  # model folder -> lock: one thread downloads one model
_LOCKS_LOCK = threading.Lock()
_OWN_MODELS_LOCK = threading.Lock()


class ModelError(providers.ProviderError):
    """A model cannot be downloaded, found or added; the text is fit to show to the user."""


class DiskSpaceError(ModelError):
    """No disk space or no access to the models folder: another address will not help."""


class _NotDownloaded(ModelError):
    """The file did not arrive intact from any address: the model archive is left, if there is one."""


class _BrokenArchive(ModelError):
    """The model archive is wrong: delete it and download again."""


class DownloadCancelled(Exception):
    """The download was stopped; .part stays for resuming."""


@dataclasses.dataclass(frozen=True)
class File:
    name: str
    size: int = 0  # 0 is unknown: such a file is not downloaded
    sha256: str = ""  # empty: only the size is checked (small files from Hugging Face)
    addresses: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class ModelKind:
    """How sherpa-onnx opens the model: the OfflineRecognizer constructor, its file parameters (parameter -> file
    role) and other parameters; with_language: the meeting language goes as the language parameter. flag_ is a model
    name pattern: CTC models have the same file set, their kind is clear only from the name."""

    method: str
    files: dict[str, str]
    flag_: str = ""
    other: dict = dataclasses.field(default_factory=dict)
    with_language: bool = False


_TRANSDUCER = {"encoder": "encoder", "decoder": "decoder", "joiner": "joiner", "tokens": "tokens"}
_CTC = {"model": "model", "tokens": "tokens"}
# The first kind whose files exist and whose flag_ matches the name is taken. Whisper token timestamps exist only
# in an export with attention outputs, otherwise segment (phrase) timestamps are used
KINDS = {
    "whisper": ModelKind(
        "from_whisper",
        {"encoder": "encoder", "decoder": "decoder", "tokens": "tokens"},
        "whisper",
        {"enable_token_timestamps": True, "enable_segment_timestamps": True},
        with_language=True,
    ),
    "nemo_transducer": ModelKind(
        "from_transducer", _TRANSDUCER, "nemo|giga-?am|parakeet", {"model_type": "nemo_transducer"}
    ),
    "transducer": ModelKind("from_transducer", _TRANSDUCER),
    "paraformer": ModelKind("from_paraformer", {"paraformer": "model", "tokens": "tokens"}, "paraformer"),
    "sense_voice": ModelKind("from_sense_voice", _CTC, "sense-?voice", {"use_itn": True}),
    "zipformer_ctc": ModelKind("from_zipformer_ctc", _CTC, "zipformer-?ctc"),
    "nemo_ctc": ModelKind("from_nemo_ctc", _CTC, "nemo|giga-?am"),
}


@dataclasses.dataclass(frozen=True)
class Model:
    id: str
    title: str
    kind: str = ""  # a KINDS key; for helper models silero_vad, pyannote, speaker_embedding
    languages: tuple[str, ...] = ()  # "*" is many languages, empty is unknown
    punctuation: bool = False
    recommended: bool = False
    hint: str = ""
    license: str = ""
    url: str = ""
    service_one: bool = False  # not shown in the window; downloaded with the main model or before recognition
    files: tuple[File, ...] = ()
    archive: File | None = None  # fallback: a .tar.bz2 archive with the same files
    custom: bool = False  # not from the catalog: from Hugging Face


@dataclasses.dataclass(frozen=True)
class _Outcome:
    """A curl attempt: exit code, HTTP code, proxy response code and the tail of curl messages."""

    exit_: int
    code: int = 0
    proxy: int = 0
    error: str = ""


@dataclasses.dataclass
class _Download:
    place: pathlib.Path
    mirror: str = ""
    cancel: threading.Event | None = None
    progress: Callable[[int, int], None] | None = None
    pauses: tuple[float, ...] = dataclasses.field(default_factory=lambda: PAUSES)
    total: int = 0
    done: int = 0  # bytes in files already done
    dead: dict[str, str] = dataclasses.field(default_factory=dict)  # host -> reason: not tried again in this download

    def report(self, now: int) -> None:
        if self.progress:
            self.progress(self.done + now, self.total)


def _file(d: dict, custom: bool = False) -> File:
    file = File(
        str(d["имя"]),
        int(d.get("размер") or 0),
        str(d.get("sha256") or "").lower(),
        tuple(str(a) for a in d.get("адреса", ())),
    )
    if (
        not FILE_NAME_PATTERN.fullmatch(file.name)
        or file.name == MARKER
        or (custom and not file.name.lower().endswith(SAFE_SUFFIXES))
    ):
        raise ModelError(tr("Invalid model file name: {name}", name=file.name))
    return file


def _model(d: dict, custom: bool = False) -> Model:
    """A model from a catalog record or свои.json; extra fields do not matter. A custom model is checked like a
    Hugging Face answer: only files without code, a kind from KINDS, no archive."""
    fields = {field.name for field in dataclasses.fields(Model)}
    d = {k: v for k, v in disk_names.from_disk(d).items() if k in fields}
    if not ID_PATTERN.fullmatch(str(d.get("id", ""))) or (custom and (d.get("kind") not in KINDS or d.get("archive"))):
        raise ModelError(tr("Invalid model record: {id_}", id_=d.get("id", "")))
    return Model(
        **(
            d
            | {
                "languages": tuple(d.get("languages", ())),
                "custom": custom,
                "files": tuple(_file(f, custom) for f in d.get("files", ())),
                "archive": _file(d["archive"]) if d.get("archive") else None,
            }
        )
    )


@functools.cache
def _catalog_data() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def catalog() -> list[Model]:
    return [_model(d) for d in _catalog_data()["модели"]]


def _set_aside(path: pathlib.Path) -> None:
    log.warning("%s is corrupted, set aside to %s.bad", path.name, path.name)
    with contextlib.suppress(OSError):
        path.replace(path.with_name(path.name + ".bad"))


def _own_entries(folder: pathlib.Path) -> list:
    """Records of свои.json; no file gives empty, a corrupted one is set aside to .bad, an unreadable one is
    ModelError."""
    path = folder / OWN_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except ValueError:
        _set_aside(path)
        return []
    except OSError as error:
        log.warning("cannot read свои.json: %s", error)
        raise ModelError(tr(NO_ACCESS, folder=folder)) from error
    recordings = data.get("модели") if isinstance(data, dict) else None
    if not isinstance(recordings, list):
        _set_aside(path)
        return []
    return recordings


def custom_items(folder: pathlib.Path) -> list[Model]:
    result = []
    for recording in _own_entries(folder):
        try:
            result.append(_model(recording, custom=True))
        except (ModelError, AttributeError, KeyError, TypeError, ValueError) as error:
            log.warning("custom model record skipped: %s", error)
    return result


def all_(folder: pathlib.Path) -> list[Model]:
    return catalog() + custom_items(folder)


def find(id_: str, folder: pathlib.Path) -> Model:
    for model in all_(folder):
        if model.id == id_:
            return model
    raise ModelError(tr("Model {id_} is neither in the catalog nor among your own models", id_=id_))


def supports_gpu(kind: str, model_id: str) -> bool:
    """Windows builds enable DirectML for GigaAM speech recognition."""
    return sys.platform == "win32" and kind == "nemo_ctc" and model_id.startswith("gigaam-")


def service_items(folder: pathlib.Path, diarization: bool = False) -> list[Model]:
    """Helper models for speech recognition (splitting by pauses); with speaker diarization, its models too."""
    data = _catalog_data()
    return [find(id_, folder) for id_ in [*data["речь"], *(data["разметка"] if diarization else [])]]


def needed_models(model: Model, folder: pathlib.Path, diarization: bool = False) -> list[Model]:
    """What else to download for the model to work: the helper models and the model itself."""
    return [m for m in [*service_items(folder, diarization), model] if status(folder, m) != DOWNLOADED]


def size(model: Model) -> int | None:
    """Total size of the model files; None if it is unknown (a model from the site list, details not received yet)."""
    if not model.files or not all(f.size for f in model.files):
        return None
    return sum(f.size for f in model.files)


def understands(model: Model, language: str) -> bool:
    """Whether the model understands the meeting language; yes if the language is not set or the model languages are
    unknown."""
    return not language or not model.languages or "*" in model.languages or language in model.languages


def roles(names: Iterable[str]) -> dict[str, str]:
    """File role for sherpa-onnx by name: tokens, encoder, decoder, joiner, model; of two precisions, int8.
    A single .onnx without other roles (helper models) is model."""
    names = list(names)
    result = {}
    for role, template in ROLES.items():
        matching = sorted(i for i in names if template.fullmatch(i.lower()))
        if matching:
            result[role] = next((i for i in matching if ".int8." in i.lower()), matching[0])
    onnx = [i for i in names if i.lower().endswith(".onnx")]
    if not result and len(onnx) == 1:
        result["model"] = onnx[0]
    return result


def pick_out(names: Iterable[str], name: str) -> tuple[str, list[str]]:
    """Model kind by its name and file set, and which files to download: the engine files of this kind and settings
    (.txt, .json, .yaml). Other .onnx files (another precision, other models) and files with code are not taken. An
    unclear kind gives empty."""
    safe = [i for i in names if i.lower().endswith(SAFE_SUFFIXES)]
    exists = roles(safe)
    kind = next(
        (
            key_
            for key_, v in KINDS.items()
            if set(v.files.values()) <= exists.keys() and re.search(v.flag_, name.lower())
        ),
        "",
    )
    to_engine = {exists[r] for r in KINDS[kind].files.values()} if kind else set()
    return kind, [i for i in safe if i in to_engine or not i.lower().endswith(".onnx")]


def languages_by_name(name: str, kind: str) -> tuple[str, ...]:
    """Model languages by its name: sherpa-onnx puts language codes into the name, Whisper without ".en" understands
    many languages; for models without languages in the name they are listed in the catalog."""
    for template, languages in _catalog_data()["языки"].items():
        if template in name.lower():
            return tuple(languages)
    codes = [LANGUAGE_WORDS.get(word, word) for word in re.split(r"[-_.]", name.lower())]
    result = tuple(dict.fromkeys(k for k in codes if k in LANGUAGES or k == "*"))
    if "*" in result:
        return ("*",)
    return result or (("*",) if kind == "whisper" else ())


def title_by_name(name: str) -> str:
    """A human title from the sherpa-onnx model name: without the date, languages (they are shown separately),
    compression and service words; the model kind (CTC, Transducer) goes last."""
    text = re.sub(r"\d{4}-\d{2}-\d{2}|\d{8}", "", name)
    for before, became in (("giga-am", "GigaAM"), ("sense-voice", "SenseVoice"), ("fast-conformer", "FastConformer")):
        text = re.sub(before, became, text, flags=re.IGNORECASE)
    words = []
    for word in re.split(r"[-_/]", text):
        word = re.sub(r"\.en$", "", word, flags=re.IGNORECASE)
        if word and word.lower() not in {*EXTRA_WORDS, *LANGUAGES} and not re.fullmatch(r"\d{4,}|\d+k", word):
            words.append(TITLE_WORDS.get(word.lower(), word))
    words.sort(key=lambda s: s in ("CTC", "Transducer"))
    return " ".join(words) or name


def engine_files(folder: pathlib.Path, model: Model) -> dict[str, pathlib.Path]:
    """Paths to the model files by role (tokens, encoder, decoder, joiner, model), from its file list."""
    return {role: folder / model.id / name for role, name in roles(f.name for f in model.files).items()}


# State on disk


def _size_of(path: pathlib.Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _label(place: pathlib.Path) -> dict | None:
    """Marker of a downloaded model; None if there is no marker, {} if it is corrupted."""
    path = place / MARKER
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _file_intact(place: pathlib.Path, file: File, recording: object) -> bool:
    """The file is in place with the size from the marker, and the marker is about this file: the size as in the
    model list."""
    path = place / file.name
    return (
        isinstance(recording, dict)
        and path.is_file()
        and path.stat().st_size == recording.get("размер")
        and (not file.size or file.size == recording.get("размер"))
    )


def _files_intact(place: pathlib.Path, model: Model, label: dict) -> bool:
    recorded_amount = label.get("файлы")
    return isinstance(recorded_amount, dict) and all(
        _file_intact(place, f, recorded_amount.get(f.name)) for f in model.files
    )


def status(folder: pathlib.Path, model: Model) -> str:
    """DOWNLOADED; BROKEN if the marker exists but a file is missing or has the wrong size; PARTIAL if there is no
    marker but there is a .part or some files; NONE. The size check takes milliseconds, sha256 is checked only on
    download."""
    place = folder / model.id
    label = _label(place)
    if label is not None:
        return DOWNLOADED if _files_intact(place, model, label) else BROKEN
    return PARTIAL if place.is_dir() and any(place.iterdir()) else NONE


def size_on_disk(folder: pathlib.Path, model: Model | None = None) -> int:
    """Space the model takes, with .part files; without a model, the whole models folder."""
    place = folder / model.id if model else folder
    return sum(p.stat().st_size for p in place.rglob("*") if p.is_file()) if place.is_dir() else 0


def free(folder: pathlib.Path) -> int:
    """Free space on the models folder disk; if the folder does not exist yet, on the disk of its nearest existing
    parent."""
    path = folder
    while not path.exists() and path != path.parent:
        path = path.parent
    return shutil.disk_usage(path).free


def size_text(bytes_: int) -> str:
    """Text like "230 MB", "1,2 GB"."""
    if bytes_ < 1024**3:
        return tr("{n} MB", n=max(1, round(bytes_ / 1024**2)) if bytes_ else 0)
    return tr("{n} GB", n=f"{bytes_ / 1024**3:.1f}".replace(".", ","))


def delete(folder: pathlib.Path, model: Model) -> None:
    """Delete all model files, with .part files. The marker goes first: a half-deleted model does not count as
    downloaded."""
    place = folder / model.id
    try:
        (place / MARKER).unlink(missing_ok=True)
        shutil.rmtree(place)
    except FileNotFoundError:
        return
    except OSError as error:
        log.warning("cannot delete model %s: %s", model.id, error)
        raise ModelError(
            tr("Could not delete the model: its files are in use. Wait for the transcription to finish and try again.")
        ) from error


def _in_use(name: str) -> ModelError:
    return ModelError(
        tr(
            "Could not save {name}: the file is in use by another program, possibly an antivirus. Try again in a "
            "minute.",
            name=name,
        )
    )


def _replace(source_of: pathlib.Path, where_to: pathlib.Path) -> None:
    try:
        transcript.replace(source_of, where_to, REPLACE_TRIES, 2.0)  # a fresh model file: the antivirus reads it
    except PermissionError as error:
        log.warning("cannot replace %s: %s", where_to, error)
        raise _in_use(where_to.name) from error


def _create(place: pathlib.Path) -> None:
    try:
        place.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        log.warning("cannot create folder %s: %s", place, error)
        raise ModelError(tr(NO_ACCESS, folder=place.parent)) from error


def _write_json(path: pathlib.Path, data: dict) -> None:
    _create(path.parent)
    try:
        transcript.write_json(path, data)
    except OSError as error:
        log.warning("cannot write %s: %s", path, error)
        if isinstance(error, PermissionError) and error.filename2:  # os.replace: the file is held
            raise _in_use(path.name) from error
        raise ModelError(tr(NO_ACCESS, folder=path.parent)) from error


# Download


def mirror_address(address: str) -> str:
    """Hugging Face mirror address: scheme://host[:port][/path] without credentials, query and fragment; empty means
    no mirror. Without https only this computer or a local network."""
    if not address.strip():
        return ""
    parsed = urllib.parse.urlsplit(address.strip())
    try:
        host, port = parsed.hostname or "", parsed.port
    except ValueError:
        host, port = "", None
    host = f"[{host}]" if ":" in host else host
    result = f"{parsed.scheme.lower()}://{host}{f':{port}' if port else ''}{parsed.path.rstrip('/')}"
    if result == HF:
        return ""
    if parsed.scheme.lower() not in ("https", "http") or not host or providers.insecure_address(result):
        raise ModelError(
            tr(
                "The mirror address {address} does not fit: it must start with https://, for example "
                "https://hf-mirror.com",
                address=address.strip(),
            )
        )
    return result


def addresses(file: File, mirror: str = "") -> list[str]:
    """File addresses in order: Hugging Face addresses via the mirror (if set), then the catalog addresses."""
    base = mirror_address(mirror)
    via_mirror = [base + a.removeprefix(HF) for a in file.addresses if base and a.startswith(HF + "/")]
    return via_mirror + list(file.addresses)


def _host(address: str) -> str:
    return urllib.parse.urlsplit(address).hostname or address


def _http_reason(code: int, host: str) -> str:
    if code in (401, 403):
        return tr(
            "{host} does not give the file without signing in (code {code}): the model is gated.", host=host, code=code
        )
    if code in (404, 410):
        return tr("{host} has no such file (code {code}).", host=host, code=code)
    if code == 429:
        return tr("Hugging Face asks to wait: too many requests. Try again in a few minutes.")
    if code >= 500:
        return tr("{host} has a temporary failure (code {code}). Try again later.", host=host, code=code)
    return tr("{host} answered with code {code}.", host=host, code=code)


def _reason(result: _Outcome, address: str, place: pathlib.Path) -> str:
    """Why this address failed, in plain text. If the file cannot be written, DiskSpaceError at once."""
    host = _host(address)
    if result.exit_ == 23:
        if free(place) < 2**28:
            raise DiskSpaceError(tr("Could not write the model file: the disk is full. Free up space and try again."))
        raise DiskSpaceError(tr(NO_ACCESS, folder=place.parent))
    if 407 in (result.proxy, result.code):
        return tr(
            "The network proxy requires signing in (code 407): curl could not sign in with the Windows account. Try "
            "another network or set a mirror."
        )
    if result.exit_ in CURL_REASONS:
        return tr(CURL_REASONS[result.exit_], host=host)
    if result.exit_ not in (0, 22):  # 22 is an HTTP refusal with -f, its code is below
        return str(providers.link(result.exit_, result.error, address))
    return _http_reason(result.code, host)


def _check_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled


def _attempt(z: _Download, address: str, file: File) -> _Outcome:
    """One curl run with resume into <name>.part. The model folder is the curl working folder, the command has only
    the file name: curl for Windows may not understand a path with Cyrillic. -f: an error response body (404, 500) is
    not written into .part; curl does not treat a 416 answer to a resume as an error."""
    if not address or providers.insecure_address(address):
        raise ModelError(
            tr("The address {address} is not https: the model file could be swapped on the way.", address=address)
        )
    part = z.place / (file.name + PART)
    command = [
        providers.CURL,
        "-q",
        "-sS",
        "-f",
        "-L",
        *providers.connection_args(address),
        "--max-redirs",
        "10",
        "-C",
        "-",
        "--speed-limit",
        "1024",
        "--speed-time",
        "60",
        "--max-filesize",
        str(file.size + 2**20),
        "-o",
        part.name,
        "-w",
        "%{http_code} %{http_connect}",
        address,
    ]

    def tick() -> None:  # the .part size as progress; cancel raises DownloadCancelled and kills curl
        _check_cancelled(z.cancel)
        z.report(_size_of(part))

    exit_, output, messages = providers.run_program(command, cwd=z.place, tick=tick)
    z.report(_size_of(part))
    codes = [int(k) if k.isdigit() else 0 for k in output.decode("ascii", "replace").split()] + [0, 0]
    return _Outcome(exit_, codes[0], codes[1], messages.decode("utf-8", "replace").strip()[-300:])


def _wait(z: _Download, pause: float) -> None:
    if z.cancel is None:
        time.sleep(pause)
    elif z.cancel.wait(pause):
        raise DownloadCancelled


def _offline(result: _Outcome, before: int, part: pathlib.Path) -> int:
    """How much the attempt looks like "host not responding": 2 drops the address at once, 1 allows one retry."""
    if result.exit_ in NO_HOST:
        return 2
    return 1 if result.exit_ in NOT_RESPONDING and _size_of(part) == before else 0


def _from_address(z: _Download, address: str, file: File) -> None:
    """Attempts with resume and z.pauses between them; a host that does not respond gets at most one retry and is
    not tried again in this download. On failure, ModelError with the reason."""
    part, link = z.place / (file.name + PART), 0
    for number, pause in enumerate((*z.pauses, None), 1):
        before = _size_of(part)
        result = _attempt(z, address, file)
        if result.exit_ == 0 and result.code in (200, 206, 416):  # 416 to a resume: .part is full, check next
            return
        log.warning(
            "download %s: %s, attempt %d, curl %d, HTTP %d: %s",
            file.name,
            _host(address),
            number,
            result.exit_,
            result.code,
            result.error,
        )
        reason = _reason(result, address, z.place)
        if result.exit_ == 33:  # the server cannot resume: the next attempt starts over
            part.unlink(missing_ok=True)
        link += _offline(result, before, part)
        if pause is None or result.code in ADDRESS_END_CODES or result.exit_ in FINAL_EXIT_CODES or link >= 2:
            if link:
                z.dead[_host(address)] = reason
            raise ModelError(reason)
        _wait(z, pause)


def _hash(path: pathlib.Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _accept_file(part: pathlib.Path, file: File, source_of: str) -> str:
    """A downloaded or unpacked .part goes into place if the size and sha256 match; returns sha256. Otherwise .part
    is deleted, the log gets what was expected and what arrived, the user gets ModelError."""
    received = _size_of(part)
    hash_ = _hash(part) if received == file.size else ""
    if hash_ and (not file.sha256 or hash_ == file.sha256):
        _replace(part, part.with_name(file.name))
        return hash_
    web = False
    with contextlib.suppress(OSError), part.open("rb") as stream:
        web = stream.read(64).lstrip().startswith(b"<")
    log.warning(
        "%s from %s does not match: size %d of %d, sha256 %s, expected %s",
        file.name,
        source_of,
        received,
        file.size,
        hash_ or "-",
        file.sha256 or "-",
    )
    part.unlink(missing_ok=True)
    raise ModelError(tr(WEB_PAGE if web else BROKEN_TEXT))


def _download_file(z: _Download, file: File) -> str:
    """The file from the first address it arrived intact from; returns its sha256. Reasons from all addresses add
    up."""
    reasons = []
    for address in addresses(file, z.mirror):
        if (host := _host(address)) in z.dead:
            reasons.append(z.dead[host])
            continue
        try:
            _from_address(z, address, file)
            return _accept_file(z.place / (file.name + PART), file, address)
        except DiskSpaceError:
            raise
        except ModelError as error:
            reasons.append(str(error))
    reason = " ".join(dict.fromkeys(reasons)) or tr("The file has no download addresses.")
    raise _NotDownloaded(tr("Could not download {name}. {reason}", name=file.name, reason=reason))


def _existing_file(place: pathlib.Path, file: File) -> str:
    """sha256 of a file already in place (downloaded earlier or put by hand) if it is intact; a corrupted one is
    deleted."""
    path = place / file.name
    if not path.is_file():
        return ""
    hash_ = _hash(path) if _size_of(path) == file.size else ""
    if not hash_ or (file.sha256 and hash_ != file.sha256):
        path.unlink()
        return ""
    return hash_


def _remaining(place: pathlib.Path, files: Iterable[File]) -> int:
    """Space needed for the files with a margin; the already downloaded part in .part is not counted."""
    return int(sum(max(f.size - _size_of(place / (f.name + PART)), 0) for f in files) * MARGIN)


def _check_space(place: pathlib.Path, need: int) -> None:
    exists = free(place)
    if need > exists:
        raise DiskSpaceError(
            tr(
                "The model needs {need}, the disk has {exists} free. Free up space and try again.",
                need=size_text(need),
                exists=size_text(exists),
            )
        )


def _extract(archive: tarfile.TarFile, member: tarfile.TarInfo, z: _Download, file: File) -> str:
    """A file from the archive under its own name: the path inside the archive is not used, so it cannot leave the
    model folder."""
    if member.size != file.size:
        raise _BrokenArchive(tr(BROKEN_TEXT))
    part = z.place / (file.name + PART)
    with archive.extractfile(member) as source_of, part.open("wb") as where_to:
        while chunk := source_of.read(2**20):
            _check_cancelled(z.cancel)
            where_to.write(chunk)
    try:
        return _accept_file(part, file, "the archive")
    except ModelError as error:
        raise _BrokenArchive(str(error)) from error


def _unpack(path: pathlib.Path, z: _Download, files: list[File]) -> dict[str, str]:
    """Needed files from the archive by name, with the sha256 check; returns their sha256. A wrong archive raises
    _BrokenArchive; a busy file (antivirus) raises ModelError and the archive stays."""
    needed_all, hashes = {f.name: f for f in files}, {}
    try:
        with tarfile.open(path) as archive:
            for member in archive:
                _check_cancelled(z.cancel)
                name = member.name.rsplit("/", 1)[-1]
                if member.isfile() and name in needed_all and name not in hashes:
                    hashes[name] = _extract(archive, member, z, needed_all[name])
    except PermissionError as error:
        log.warning("cannot unpack archive %s: %s", path.name, error)
        raise ModelError(
            tr(
                "Could not unpack the model: the file is held by another program, perhaps an antivirus. Try again in a "
                "minute."
            )
        ) from error
    except (tarfile.TarError, EOFError, OSError) as error:
        log.warning("archive %s is corrupted: %s", path.name, error)
        if isinstance(error, OSError) and error.errno == errno.ENOSPC:
            raise DiskSpaceError(tr("The disk ran out of space for the model. Free up space and try again.")) from error
        raise _BrokenArchive(tr("The model archive arrived damaged. Try again later.")) from error
    if none_ := sorted(needed_all.keys() - hashes.keys()):
        raise _BrokenArchive(tr("The model archive has no files: {names}", names=", ".join(none_)))
    return hashes


def _from_archive(z: _Download, archive: File, files: list[File]) -> dict[str, str]:
    """Fallback: the archive (k2-fsa releases on GitHub), only the missing files from it. Archive progress counts as
    a share of the missing files. The archive is deleted afterwards, a corrupted one too; on an access error it
    stays."""
    _check_space(z.place, _remaining(z.place, [archive, *files]))
    if not _existing_file(z.place, archive):
        balance, done, total, progress = sum(f.size for f in files), z.done, z.total, z.progress
        share = progress and (
            lambda done_count, _of: progress(done + balance * done_count // max(archive.size, 1), total)
        )
        _download_file(dataclasses.replace(z, progress=share, done=0, total=archive.size), archive)
    path = z.place / archive.name
    try:
        hashes = _unpack(path, z, files)
    except _BrokenArchive:
        path.unlink(missing_ok=True)
        raise
    path.unlink(missing_ok=True)
    return hashes


def _download_files(z: _Download, model: Model, files: list[File]) -> dict[str, str]:
    """Files by their addresses; those that did not download come from the model archive, if there is one."""
    hashes = {}
    for file in files:
        _check_cancelled(z.cancel)
        try:
            hashes[file.name] = _download_file(z, file)
            z.done += file.size
        except _NotDownloaded:
            if model.archive is None:
                raise
    if others_ := [f for f in files if f.name not in hashes]:
        hashes |= _from_archive(z, model.archive, others_)
    return hashes


def _remove_extra(place: pathlib.Path, model: Model) -> None:
    """Files of an older model version that are not in its list: the engine would take them instead of the new
    ones."""
    needed_all = {f.name for f in model.files}
    for path in place.iterdir():
        if path.is_file() and path.name not in needed_all:
            path.unlink(missing_ok=True)


def _lock(place: pathlib.Path) -> threading.Lock:
    with _LOCKS_LOCK:
        return _LOCKS.setdefault(str(place).lower(), threading.Lock())


def _download(z: _Download, model: Model) -> None:
    """Missing model files by address, what failed comes from the archive; the marker goes last. One thread
    downloads one model: a second one waits and then sees it downloaded."""
    if size(model) is None:
        raise ModelError(tr(NO_SIZE, title=tr(model.title)))
    with _lock(z.place):
        if status(z.place.parent, model) == DOWNLOADED:
            return
        _create(z.place)
        (z.place / MARKER).unlink(missing_ok=True)  # while downloading: PARTIAL, not BROKEN
        hashes = {f.name: _existing_file(z.place, f) for f in model.files}
        missing = [f for f in model.files if not hashes[f.name]]
        z.total = size(model) or 0
        z.done = z.total - sum(f.size for f in missing)
        _check_space(z.place, _remaining(z.place, missing))
        hashes |= _download_files(z, model, missing)
        _remove_extra(z.place, model)
        _write_json(
            z.place / MARKER, {"файлы": {f.name: {"размер": f.size, "sha256": hashes[f.name]} for f in model.files}}
        )


def _in_turn(folder: pathlib.Path, list_: list[Model], template: _Download) -> None:
    total, done = sum(size(m) or 0 for m in list_), 0
    for model in list_:
        _check_cancelled(template.cancel)
        progress = template.progress and (lambda done_count, _of, b=done: template.progress(b + done_count, total))
        _download(dataclasses.replace(template, place=folder / model.id, progress=progress), model)
        done += size(model) or 0


def download_all(
    folder: pathlib.Path,
    list_: list[Model],
    progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
    mirror: str = "",
) -> None:
    """Download models in turn. Files already in place are checked and not downloaded again. progress(done, total)
    is in bytes over all models. Cancel stops curl, .part stays for resuming: DownloadCancelled. Errors raise
    ModelError, lack of space raises DiskSpaceError. The mirror replaces Hugging Face addresses and goes first."""
    _in_turn(folder, list_, _Download(folder, mirror_address(mirror), cancel, progress))


def download_file(
    place: pathlib.Path,
    file: File,
    progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
) -> pathlib.Path:
    """One file into place with resume, retries and the size and sha256 check (also program updates); its path."""
    _create(place)
    if not _existing_file(place, file):
        _check_space(place, _remaining(place, [file]))
        _download_file(_Download(place, cancel=cancel, progress=progress, total=file.size), file)
    return place / file.name


def download(
    folder: pathlib.Path,
    model: Model,
    progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
    mirror: str = "",
) -> None:
    download_all(folder, [model], progress, cancel, mirror)


def fetch_missing(
    folder: pathlib.Path, list_: list[Model], mirror: str = "", cancel: threading.Event | None = None
) -> None:
    """One attempt per address, no pauses: helper models before recognition, the meeting does not wait for
    minutes."""
    _in_turn(folder, list_, _Download(folder, mirror_address(mirror), cancel, None, pauses=()))


# Models from Hugging Face


def parse_link(url: str) -> tuple[str, str, str]:
    """Parses "owner/model" or a Hugging Face link: the model page, /tree/<revision>/<folder>, a file link
    (/blob/, /resolve/) -> (repository, revision, folder). No revision means main."""
    text = url.strip()
    if REPOSITORY.fullmatch(text):
        return text, "main", ""
    parsed = urllib.parse.urlsplit(text if "://" in text else "https://" + text)
    parts = [urllib.parse.unquote(ch) for ch in parsed.path.split("/") if ch]
    repository = "/".join(parts[:2])
    if (
        (parsed.hostname or "").lower() not in HF_HOSTS
        or not REPOSITORY.fullmatch(repository)
        or parts[0] in ("datasets", "spaces")
    ):
        raise ModelError(tr(NOT_A_LINK))
    if len(parts) < 4 or parts[2] not in ("tree", "blob", "resolve"):
        return repository, "main", ""
    path = parts[4:] if parts[2] == "tree" else parts[4:-1]  # for a file link, its folder
    if not all(FILE_NAME_PATTERN.fullmatch(ch) for ch in path):
        raise ModelError(tr(NOT_A_LINK))
    return repository, parts[3], "/".join(path)


def _hf_response(base: str, path: str) -> tuple[int, object]:
    """HTTP code and parsed JSON (None if not JSON, the start of the body goes to the log)."""
    code, text = providers.fetch(base + path)
    if code != 200:
        log.warning("Hugging Face %s: code %d", base + path, code)
        return code, None
    try:
        return code, json.loads(text)
    except ValueError:
        log.warning("Hugging Face %s: not JSON: %r", base + path, text[:200])
        return code, None


def _api(path: str, mirror: str, repository: str = "") -> object:
    """Hugging Face API answer. huggingface.co comes first: the mirror also serves the files themselves, so they
    cannot be checked against its details; the mirror is a fallback, noted in the log. An empty repository means the
    model list: a huggingface.co refusal also leads to the mirror, and the error text is only the reason."""
    reasons = []
    for base in dict.fromkeys(b for b in (HF, mirror_address(mirror)) if b):
        try:
            code, data = _hf_response(base, path)
        except providers.ProviderError as error:
            log.warning("Hugging Face %s: %s", base + path, error)
            reasons.append(str(error))
            continue
        if data is not None:
            if base != HF:
                log.warning("details of %s taken from mirror %s: huggingface.co did not answer", path, base)
            return data
        if base == HF and repository and code in (401, 403, 404):  # without sign-in HF gives 401 for a missing one
            raise ModelError(tr(CLOSED if code == 403 else MISSING_OR_PRIVATE, repository=repository))
        reasons.append(tr(UNCLEAR_ANSWER) if code == 200 else _http_reason(code, _host(base)))
    reason = " ".join(dict.fromkeys(reasons))
    raise ModelError(
        tr("Hugging Face did not return the model details: {reason}", reason=reason) if repository else reason
    )


def _hf_file(recording: object, address: str, prefix: str) -> File | None:
    """A file from the model file list (siblings) in a Hugging Face answer: only safe ones and only from the model
    folder, not nested. Files in LFS have sha256, small ones only a size, and the list of all models has no size."""
    if not isinstance(recording, dict):
        return None
    path = str(recording.get("rfilename", ""))
    name = path.removeprefix(prefix)
    if (
        not path.startswith(prefix)
        or not name.lower().endswith(SAFE_SUFFIXES)
        or not FILE_NAME_PATTERN.fullmatch(name)
        or name == MARKER
    ):
        return None
    lfs = recording.get("lfs") if isinstance(recording.get("lfs"), dict) else {}
    sha256 = str(lfs.get("sha256") or "").lower()
    return File(
        name,
        int(lfs.get("size") or recording.get("size") or 0),
        sha256 if SHA256.fullmatch(sha256) else "",
        (f"{address}/{urllib.parse.quote(path)}",),
    )


def _card(info: dict, field: str) -> list[str]:
    """A model card field (license, language): a string or a list of strings."""
    card = info.get("cardData") if isinstance(info.get("cardData"), dict) else {}
    value = card.get(field) or []
    return [value] if isinstance(value, str) else [z for z in value if isinstance(z, str)]


def _id(repository: str, subfolder: str) -> str:
    """Folder name of a Hugging Face model: short, since a Windows path is limited to 260 characters."""
    base_name = re.sub(r"[^a-z0-9._-]+", "-", f"{repository}-{subfolder}".lower().replace("/", "--")).strip("-.")
    if len(base_name) > 48:
        base_name = base_name[:40].rstrip("-.") + "-" + hashlib.sha256(base_name.encode()).hexdigest()[:7]
    return "hf-" + base_name


def hf_model(info: dict, subfolder: str = "") -> Model | None:
    """A model from Hugging Face repository details (id, sha, siblings): the model folder files at this commit, kind
    and languages by name and files. None if it is not a speech recognition model the program understands."""
    repository, commit = str(info.get("id") or ""), str(info.get("sha") or "")
    if not REPOSITORY.fullmatch(repository) or not COMMIT.fullmatch(commit):
        return None
    address, prefix = f"{HF}/{repository}/resolve/{commit}", f"{subfolder}/" if subfolder else ""
    recordings = info.get("siblings") if isinstance(info.get("siblings"), list) else []
    files = {f.name: f for f in (_hf_file(z, address, prefix) for z in recordings) if f}
    name = "-".join(ch for ch in (repository.split("/")[1], subfolder.replace("/", "-")) if ch)
    kind, names = pick_out(files, name)
    if not kind:
        return None
    card = [me for me in _card(info, "language") if re.fullmatch("[a-z]{2,3}", me)]
    return Model(
        id=_id(repository, subfolder),
        title=title_by_name(name),
        kind=kind,
        languages=tuple(dict.fromkeys([*languages_by_name(name, kind), *card])),
        punctuation=bool(PUNCTUATION.search(name)),
        license=", ".join(_card(info, "license")),
        url=f"{HF}/{repository}",
        files=tuple(files[i] for i in names),
        custom=True,
    )


def read(url: str, mirror: str = "") -> Model:
    """A Hugging Face model by link or "owner/model" name: files with sizes and sha256 at a pinned commit in one
    request. Only files without code are taken, the kind is by name and file set."""
    repository, revision, subfolder = parse_link(url)
    info = _api(
        f"/api/models/{repository}/revision/{urllib.parse.quote(revision, safe='')}?blobs=true", mirror, repository
    )
    if not isinstance(info, dict) or info.get("gated") or info.get("private"):
        raise ModelError(tr(CLOSED, repository=repository))
    try:
        model = hf_model({"id": repository} | info, subfolder)
    except (AttributeError, TypeError, ValueError) as error:
        log.warning("cannot parse details of %s: %s", repository, error)
        raise ModelError(
            tr("Hugging Face did not return the model details: {reason}", reason=tr(UNCLEAR_ANSWER))
        ) from error
    if model is None:
        raise ModelError(
            tr(
                "{repository} has no model the program understands: it needs ONNX files and tokens.txt of sherpa-onnx "
                "models (GigaAM, Whisper, Parakeet, Zipformer, Paraformer, SenseVoice).",
                repository="/".join(ch for ch in (repository, subfolder) if ch),
            )
        )
    return model


def _record(model: Model) -> dict:
    """Model as a record of the models file, with the field names on disk."""
    data = dataclasses.asdict(model)
    data["files"] = [disk_names.to_disk(f) for f in data["files"]]
    data["archive"] = data["archive"] and disk_names.to_disk(data["archive"])
    return disk_names.to_disk(data)


def _write_own(folder: pathlib.Path, models: list[Model]) -> None:
    _write_json(folder / OWN_FILE, {"модели": [_record(m) for m in models]})


def remember(folder: pathlib.Path, model: Model) -> None:
    """A Hugging Face model goes into свои.json; the same one again updates the record. Files are not downloaded."""
    with _OWN_MODELS_LOCK:
        _write_own(folder, [*(m for m in custom_items(folder) if m.id != model.id), model])


def remove_custom(folder: pathlib.Path, id_: str) -> None:
    """Remove a custom model from the list together with its files."""
    with _OWN_MODELS_LOCK:
        recordings = custom_items(folder)
        for model in recordings:
            if model.id == id_:
                delete(folder, model)
        _write_own(folder, [m for m in recordings if m.id != id_])


def repository(model: Model) -> str:
    """The model's Hugging Face repository: by a custom model link or a catalog file address; empty if not from
    Hugging Face."""
    for address in (model.url, *(a for f in model.files for a in f.addresses)):
        parsed = urllib.parse.urlsplit(address)
        parts = [ch for ch in parsed.path.split("/") if ch][:2]
        if (address.startswith(HF + "/") or (parsed.hostname or "") in HF_HOSTS) and REPOSITORY.fullmatch(
            "/".join(parts)
        ):
            return "/".join(parts)
    return ""


def _best(models: Iterable[Model]) -> list[Model]:
    """Of the compressed (int8), full and fp16 versions of one model, the compressed one; identical titles get the
    date from the name."""
    best: dict[str, tuple[int, Model]] = {}
    for model in models:
        name = repository(model).lower()
        base_name = re.sub(r"[-_.](?:int8|fp16)(?=[-_.]|$)", "", name)
        place = 0 if "int8" in name else 2 if "fp16" in name else 1
        if base_name not in best or place < best[base_name][0]:
            best[base_name] = (place, model)
    result = [m for _, m in best.values()]
    titles = collections.Counter((m.title, m.languages, m.punctuation) for m in result)
    for number, m in enumerate(result):
        if titles[m.title, m.languages, m.punctuation] > 1 and (date := re.search(r"\d{4}-\d{2}-\d{2}", m.url)):
            result[number] = dataclasses.replace(m, title=f"{m.title} {date.group()}")
    return sorted(result, key=lambda m: m.title.lower())


def _from_site(info: object, hide_: re.Pattern) -> Model | None:
    """A model from a site list record: only from the sherpa-onnx author and not in "скрыть"; a broken one gives
    None and a log line."""
    if not isinstance(info, dict) or not str(info.get("id", "")).startswith(HF_AUTHOR + "/"):
        return None
    if hide_.search(str(info["id"])):
        return None
    try:
        return hf_model(info)
    except (AttributeError, TypeError, ValueError) as error:
        log.warning("model %s in the site list skipped: %s", info.get("id"), error)
        return None


def from_site(folder: pathlib.Path, mirror: str = "") -> list[Model]:
    """sherpa-onnx models on Hugging Face that are neither in the catalog nor among custom ones; the list has no
    file sizes, read() gives them. Models in the catalog "скрыть" list (streaming, non-commercial license) and those
    whose kind the program does not understand are not taken. An error is ModelError with one reason."""
    answer = _api(SITE, mirror)
    exists = {m.id for m in all_(folder)} | {_id(r, "") for m in catalog() if (r := repository(m))}
    hide_ = re.compile("|".join(_catalog_data()["скрыть"]), re.IGNORECASE)
    models = (_from_site(s, hide_) for s in (answer if isinstance(answer, list) else []))
    return _best(m for m in models if m and m.id not in exists)
