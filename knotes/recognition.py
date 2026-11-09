# SPDX-License-Identifier: GPL-3.0-only
"""Meeting transcript with speaker diarization.

A recording folder of the program has микрофон.flac (the owner) and собеседники.flac (computer audio), each
track is recognized separately, phrases are merged by time. A ready meeting recording (video or
audio) is taken as one shared track общая.flac.

Command line: python -m knotes.recognition <meeting folder | recording file> [--again]
"""

import bisect
import collections
import dataclasses
import datetime
import functools
import logging
import pathlib
import re
import subprocess
import tempfile
import sys
import threading
import time
from collections.abc import Callable

import numpy as np

from knotes import catalog, local_engine, models, providers, transcript, system
from knotes import settings
from knotes import vocabulary as vocab
from knotes.language import tr_key, tr

SAMPLE_RATE = 16000
BITRATE = "32k"  # 16 kHz mono speech: about 100 minutes fit into the 25 MB limit
WHOLE_UNTIL = 90 * 60
CHUNK_S = 30 * 60
OVERLAP = 20  # shared audio of neighboring chunks, used to match speaker labels
TRACKS = transcript.TRACKS
TRANSCRIPT = transcript.TRANSCRIPT
ANSWERS = transcript.ANSWERS
SOURCE = transcript.SOURCE
PARTS = "куски_распознавания.json"  # chunks of a long track recognized before a break
RETRY_AFTER_S = (10, 30)  # pauses before retrying a request after a temporary failure
LEVEL_BLOCK = 12000  # 50 ms loudness windows measured at a time: 10 minutes of audio
NO_SERVICE_MODELS = tr_key(
    "Для распознавания на этом компьютере один раз нужны служебные модели (около {size}), "
    "скачать не вышло: {reason} Подключитесь к интернету и повторите, дальше интернет не "
    "понадобится."
)
log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Params:
    provider: providers.Provider
    key: str = dataclasses.field(repr=False)  # the API key does not get into the log or debug output
    language: str = "ru"
    vocabulary: tuple[vocab.Word, ...] = ()
    retention: bool = False
    model: str = ""
    diarization: bool = True  # on this computer: tell the other participants apart
    models_folder: pathlib.Path = pathlib.Path()  # on this computer: where the downloaded models are
    mirror: str = ""  # on this computer: where to download helper models from instead of huggingface.co
    cancel: threading.Event | None = dataclasses.field(default=None, compare=False)  # stops the engine
    # on this computer: one engine process for track after track (during the meeting); None: a process per track
    engine: local_engine.Server | None = dataclasses.field(default=None, compare=False, repr=False)

    @property
    def final_model(self) -> str:
        return self.model or self.provider.model


def params(config: settings.Settings) -> Params:
    provider = providers.connection("распознавание", config.provider, config.address)
    model = config.local_model if provider.local_engine else config.model
    return Params(
        provider,
        settings.address_key(provider.address, provider.key, provider.variable),
        config.language,
        tuple(vocab.from_data(config.vocabulary)),
        config.provider_retention,
        model.strip(),
        config.speaker_diarization,
        settings.models_folder(),
        config.models_mirror,
    )


def done(params_: Params) -> bool:
    """Whether a meeting can be recognized now: the model on this computer is downloaded, or the service has a key."""
    if params_.provider.local_engine:
        try:
            model = models.find(params_.final_model, params_.models_folder)
        except models.ModelError:
            return False
        return models.status(params_.models_folder, model) == models.DOWNLOADED
    return bool(params_.key) or not params_.provider.key_needed


def _ffmpeg(*arguments: str, input_: bytes | None = None) -> bytes:
    """ffmpeg without a console window: otherwise a windowed program opens a black window on every call."""
    command = [system.ffmpeg(), "-hide_banner", "-loglevel", "error", *arguments]
    try:
        return subprocess.run(
            command, input=input_, capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW
        ).stdout
    except subprocess.CalledProcessError as error:
        _ffmpeg_failed(error)
        raise


def _ffmpeg_failed(error: subprocess.CalledProcessError) -> None:
    log.warning("ffmpeg failed (%d): %s", error.returncode, (error.stderr or b"")[-2000:].decode("utf-8", "ignore"))


def _read_int16(path: pathlib.Path) -> np.ndarray:
    raw_items = _ffmpeg("-i", str(path), "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1")
    return np.frombuffer(raw_items, np.int16)


def read_mono(path: pathlib.Path) -> np.ndarray:
    audio = _read_int16(path).astype(np.float32)
    audio /= 32768  # in place: a 3-hour track is 700 MB as float32
    return audio


def write_flac(audio: np.ndarray, path: pathlib.Path) -> None:
    raw_items = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    _ffmpeg(
        "-y",
        "-f",
        "s16le",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-c:a",
        "flac",
        str(path),
        input_=raw_items,
    )


def join_flac(chunks: list[tuple[int, pathlib.Path]], path: pathlib.Path) -> None:
    """Chunks (start in samples, file) into one 16 kHz mono FLAC: gaps become silence, a chunk running into the next
    one is cut at its start. ffmpeg streams them, so a 3-hour track does not sit in memory."""
    chunks = sorted(chunks, key=lambda c: c[0])
    chunks = [c for number, c in enumerate(chunks) if number + 1 == len(chunks) or chunks[number + 1][0] > c[0]]
    inputs, filters = [], []
    for number, (start, file) in enumerate(chunks):
        inputs += ["-i", str(file)]
        steps = [f"aresample={SAMPLE_RATE}", "aformat=sample_fmts=s16:channel_layouts=mono"]
        if number + 1 < len(chunks):
            length = chunks[number + 1][0] - start
            steps += [f"atrim=end_sample={length}", f"apad=whole_len={length}"]
        if number == 0 and start > 0:
            steps.append(f"adelay=delays={start}S:all=1")
        filters.append(f"[{number}:a]{','.join(steps)}[a{number}]")
    joined = "".join(f"[a{number}]" for number in range(len(chunks)))
    filters.append(f"{joined}concat=n={len(chunks)}:v=0:a=1[out]")
    _ffmpeg("-y", *inputs, "-filter_complex", ";".join(filters), "-map", "[out]", "-c:a", "flac", str(path))


def to_mp3(path: pathlib.Path, start: float = 0, length: float = 0) -> bytes:
    """Mono MP3 of a track; start and length in seconds cut out a chunk."""
    chunk = ["-ss", str(start), "-t", str(length)] if length else []
    return _ffmpeg(
        *chunk,
        "-i",
        str(path),
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "libmp3lame",
        "-b:a",
        BITRATE,
        "-f",
        "mp3",
        "pipe:1",
    )


def audio_chunk(folder: pathlib.Path, start: float, length: float) -> bytes:
    """All meeting tracks mixed, from start for length seconds: 16 kHz mono, 16 bit, to play back the spot."""
    paths = [folder / f"{name}.flac" for name in TRACKS if (folder / f"{name}.flac").exists()]
    if not paths:
        return b""
    inputs = [a for p in paths for a in ("-ss", f"{max(0.0, start):.2f}", "-t", str(length), "-i", str(p))]
    mix = ["-filter_complex", f"amix=inputs={len(paths)}:normalize=0"] if len(paths) > 1 else []
    return _ffmpeg(*inputs, *mix, "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1")


def _recognize(params_: Params, mp3: bytes) -> dict:
    """A request to the provider; a temporary failure (connection, overload) gets two more tries with a pause."""
    request = providers.Request(
        mp3,
        params_.language,
        vocab.spellings(list(params_.vocabulary)),
        params_.retention,
        params_.model,
        cancel=params_.cancel,
    )
    for pause in (*RETRY_AFTER_S, None):
        try:
            return providers.recognize(params_.provider, params_.key, request)
        except providers.TemporaryError as error:
            if pause is None:
                raise
            log.info("temporary failure (%s), retrying in %s s", error, pause)
            _pause(params_.cancel, pause)
    raise AssertionError  # not reached: the last try either returned an answer or raised an error


def _pause(cancel: threading.Event | None, seconds: float) -> None:
    if cancel is None:
        time.sleep(seconds)
    elif cancel.wait(seconds):
        providers.stop_on(cancel)()


def _starts(duration: float) -> range:
    return range(0, max(1, int(np.ceil(duration - OVERLAP))), CHUNK_S)


def part_count(duration: float) -> int:
    """How many requests a track takes: one for the whole track or one per chunk."""
    return 1 if duration <= WHOLE_UNTIL else len(_starts(duration))


@dataclasses.dataclass
class Chunks:
    """What a track request reports and resumes from: answered(answer) runs after each provider answer (pay for
    it), save(parts) after each chunk of a long track with all its chunks so far; done are the chunks recognized
    before a break (no money left, no network), they are not sent again."""

    done: list[dict] = dataclasses.field(default_factory=list)
    answered: Callable[[dict], None] = lambda _answer: None
    save: Callable[[list[dict]], None] = lambda _parts: None


def recognize_track(
    path: pathlib.Path,
    duration: float,
    params_: Params,
    chunks: Chunks | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[dict]:
    """A track in one request; a long one or one not accepted whole goes in overlapping chunks. progress(done, total)
    is for this computer, in seconds of the track audio."""
    chunks = chunks or Chunks()
    if params_.provider.local_engine:  # the engine cuts the recording at pauses itself, there is no size limit
        result = [{"сдвиг": 0, "ответ": _local(path, params_, progress)}]
        chunks.answered(result[0]["ответ"])
        return result
    if duration <= WHOLE_UNTIL and not chunks.done:
        try:
            result = [{"сдвиг": 0, "ответ": _recognize(params_, to_mp3(path))}]
            chunks.answered(result[0]["ответ"])
            return result
        except providers.SizeError as error:
            log.info("%s was not accepted whole (%s), splitting into chunks", path.name, error)
    return _in_chunks(path, duration, params_, chunks)


def _in_chunks(path: pathlib.Path, duration: float, params_: Params, chunks: Chunks) -> list[dict]:
    known = {part["сдвиг"]: part for part in chunks.done}
    parts = []
    for s in _starts(duration):
        if s in known:
            parts.append(known[s])
            continue
        try:
            parts.append({"сдвиг": s, "ответ": _recognize(params_, to_mp3(path, s, CHUNK_S + OVERLAP))})
        except providers.SizeError as error:
            log.warning("%s: the chunk from %d s was not taken either: %s", path.name, s, error)
            raise providers.ProviderError(
                tr(
                    "{title} did not take even a {minutes}-minute piece of the recording in time. Try again later or "
                    "choose another model in Settings.",
                    title=params_.provider.title,
                    minutes=CHUNK_S // 60,
                )
            ) from error
        chunks.answered(parts[-1]["ответ"])
        chunks.save(parts)
    return parts


def _fetch_missing(none_: list[models.Model], params_: Params) -> None:
    try:
        models.fetch_missing(params_.models_folder, none_, params_.mirror, params_.cancel)
    except models.DownloadCancelled as error:
        raise local_engine.Cancelled(tr(local_engine.CANCELLED_TEXT)) from error
    except models.ModelError as error:
        log.warning("helper models %s did not download: %s", [m.id for m in none_], error)
        size = models.size_text(sum(models.size(m) or 0 for m in none_))
        raise providers.ProviderError(tr(NO_SERVICE_MODELS, size=size, reason=error)) from error


def _local_models(params_: Params) -> tuple[models.Model, dict[str, pathlib.Path]]:
    """The main model (not downloaded: refused at once) and the helper model files by their kind."""
    folder = params_.models_folder
    model = models.find(params_.final_model, folder)
    if models.status(folder, model) != models.DOWNLOADED:
        raise providers.ProviderError(tr(local_engine.NOT_DOWNLOADED, title=tr(model.title)))
    service_items = models.service_items(folder, params_.diarization)
    if none_ := [m for m in service_items if models.status(folder, m) != models.DOWNLOADED]:
        _fetch_missing(none_, params_)
    return model, {m.kind: models.engine_files(folder, m)["model"] for m in service_items}


def _local(path: pathlib.Path, params_: Params, progress: Callable[[int, int], None] | None = None) -> dict:
    """A track by the engine on this computer; missing helper models are downloaded, a missing main model is refused."""
    model, paths = _local_models(params_)
    diarization = (
        local_engine.Diarization(str(paths["pyannote"]), str(paths["speaker_embedding"]))
        if params_.diarization
        else None
    )
    files = {role: str(p_) for role, p_ in models.engine_files(params_.models_folder, model).items()}
    to_engine = local_engine.Model(
        files, model.kind, str(paths["silero_vad"]), tr(model.title), params_.language, model.id, model.custom
    )
    run = params_.engine.recognize if params_.engine else local_engine.recognize_in_process
    return run(path, to_engine, diarization, progress, params_.cancel)


def _chunk_words(part: dict) -> list[dict]:
    return [
        s | {"начало": s["начало"] + part["сдвиг"], "конец": s["конец"] + part["сдвиг"]}
        for s in providers.words(part["ответ"])
    ]


def _normalized(word: str) -> str:
    return re.sub(r"\W", "", word.lower().replace("ё", "е"))


def _match_labels(previous_items: list[dict], new_items: list[dict]) -> dict[str, str]:
    """A label of a new chunk becomes the previous label that has the most identical words at the same time."""
    voices: collections.Counter = collections.Counter()
    for n_ in new_items:
        for p in previous_items:
            if abs(n_["начало"] - p["начало"]) < 0.6 and _normalized(n_["слово"]) == _normalized(p["слово"]):
                voices[(n_["говорящий"], p["говорящий"])] += 1
                break
    mapping: dict[str, str] = {}
    for (new_one, previous_one), _ in voices.most_common():
        mapping.setdefault(new_one, previous_one)
    return mapping


def words_from(parts: list[dict]) -> list[dict]:
    """Words of all chunks on a common time scale.

    Speaker labels of a new chunk are matched to the previous ones by the overlap, duplicates in the overlap are cut
    at its middle; a label that could not be matched gets the chunk number.
    """
    result: list[dict] = []
    for number, part in enumerate(parts):
        words = _chunk_words(part)
        if number:
            from_time, until = part["сдвиг"], part["сдвиг"] + OVERLAP
            mapping = _match_labels(
                [s for s in result if s["начало"] >= from_time], [s for s in words if s["начало"] < until]
            )
            for s in words:
                s["говорящий"] = mapping.get(s["говорящий"], f"{number}:{s['говорящий']}")
            boundary = from_time + OVERLAP / 2
            words = [s for s in result if s["начало"] < boundary] + [s for s in words if s["начало"] >= boundary]
        result = words
    return result


def phrases(words: list[dict], pause: float = 2.0) -> list[dict]:
    """Consecutive words of one speaker without a long pause form one phrase."""
    result: list[dict] = []
    for s in words:
        last = result[-1] if result else None
        if last and last["говорящий"] == s["говорящий"] and s["начало"] - last["конец"] <= pause:
            last["конец"] = s["конец"]
            last["слова"].append(s["слово"])
        else:
            result.append(
                {"начало": s["начало"], "конец": s["конец"], "говорящий": s["говорящий"], "слова": [s["слово"]]}
            )
    for f in result:
        f["текст"] = re.sub(r"\s+([,.!?:;])", r"\1", " ".join(f.pop("слова")))
    return result


def loudness(audio: np.ndarray | None) -> tuple[np.ndarray, float] | None:
    """Loudness in 50 ms windows and the reference speech level; None if there is no track or it is silent. The
    audio is float or int16 (as read from the file)."""
    if audio is None or not audio.size:
        return None
    return _with_anchor(_window_levels(audio))


def _window_levels(audio: np.ndarray) -> np.ndarray:
    """Loudness of each whole 50 ms window, taken in blocks: a float copy of a whole 3-hour track is 700 MB. The tail
    shorter than a window is left out."""
    window = SAMPLE_RATE // 20
    levels = np.empty(len(audio) // window, np.float32)
    for s in range(0, levels.size, LEVEL_BLOCK):
        block = audio[s * window : min(s + LEVEL_BLOCK, levels.size) * window].reshape(-1, window).astype(np.float32)
        if audio.dtype == np.int16:
            block /= 32768
        levels[s : s + len(block)] = np.sqrt(np.square(block).mean(axis=1))
    return levels


def _with_anchor(levels: np.ndarray) -> tuple[np.ndarray, float] | None:
    anchor = float(np.percentile(levels, 95)) if levels.size else 0.0
    return (levels, anchor) if anchor > 1e-3 else None


def _measure(path: pathlib.Path) -> tuple[float, tuple[np.ndarray, float] | None]:
    """Track length in seconds and its loudness, counted from ffmpeg piece by piece: the samples of a 3-hour track
    (350 MB) are never in memory whole."""
    window_bytes = SAMPLE_RATE // 20 * 2
    command = [system.ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-ac", "1"]
    command += ["-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1"]
    parts, total, rest = [], 0, b""
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=errors, creationflags=subprocess.CREATE_NO_WINDOW
        )
        with process:
            while raw := process.stdout.read(window_bytes * LEVEL_BLOCK):  # type: ignore[union-attr]
                total, data = total + len(raw), rest + raw
                whole = len(data) // window_bytes * window_bytes
                parts.append(_window_levels(np.frombuffer(data[:whole], np.int16)))
                rest = data[whole:]
        if process.returncode:
            errors.seek(0)
            failure = subprocess.CalledProcessError(process.returncode, command, stderr=errors.read())
            _ffmpeg_failed(failure)
            raise failure
    levels = np.concatenate(parts) if parts else np.empty(0, np.float32)
    return total // 2 / SAMPLE_RATE, _with_anchor(levels)


def _average(loudness_: tuple[np.ndarray, float], start: float, end: float) -> float:
    levels, anchor = loudness_
    s, by = int(start * 20), max(int(end * 20), int(start * 20) + 1)
    chunk = levels[s:by]
    return float(chunk.mean()) / anchor if chunk.size else 0.0


def _owners(phrases_: list[dict], mic: tuple, sys_: tuple) -> set[str]:
    """Microphone track labels whose speech is louder overall in the microphone than in the computer audio.

    The other microphone labels are speaker echo, these words are already in the "others" track.
    Decided per whole speaker: echo throws off single phrases.
    """
    excess: dict[str, float] = collections.defaultdict(float)
    for f in phrases_:
        difference = _average(mic, f["начало"], f["конец"]) - _average(sys_, f["начало"], f["конец"])
        excess[f["говорящий"]] += difference * (f["конец"] - f["начало"])
    return {label for label, p in excess.items() if p > 0}


def _by_start(words: list[dict]) -> tuple[list[float], list[dict]]:
    """Words sorted by start with their starts: the words near a moment are found by bisection."""
    ordered = sorted(words, key=lambda s: s["начало"])
    return [s["начало"] for s in ordered], ordered


def _repeat(phrase: dict, others_words: tuple[list[float], list[dict]]) -> bool:
    """A microphone phrase made of the others' words at the same time is echo that the provider gave to the owner."""
    custom_items = [_normalized(s) for s in phrase["текст"].split()]
    if len(custom_items) < 3:
        return False
    starts, ordered = others_words
    first, last = bisect.bisect_left(starts, phrase["начало"] - 1), bisect.bisect_right(starts, phrase["конец"] + 1.5)
    beside = {_normalized(part) for s in ordered[first:last] for part in s["слово"].split()}
    return sum(s in beside for s in custom_items) >= 0.7 * len(custom_items)


def collect_phrases(words: dict[str, list[dict]], audio: dict[str, np.ndarray]) -> list[dict]:
    """Phrases of all tracks by time, labeled "Я" (me), "Собеседник N" (speaker N) or "Участник N" (participant N).

    "Я" comes only from the microphone track and only if the computer audio has speech;
    without it (an in-person meeting, a ready recording) the owner cannot be told apart, everyone is "Участник N".
    """
    return _collect(words, {name: loudness(audio.get(name)) for name in ("микрофон", "собеседники")})


def _collect(words: dict[str, list[dict]], levels: dict[str, tuple | None]) -> list[dict]:
    """collect_phrases by the loudness of the tracks already measured."""
    mic, sys_ = levels.get("микрофон"), levels.get("собеседники")
    result: list[dict] = []
    for track, track_words in words.items():
        phr = [f | {"говорящий": f"{track}/{f['говорящий']}"} for f in phrases(track_words)]
        if track == "микрофон" and mic and sys_:
            custom_items, others_words = _owners(phr, mic, sys_), _by_start(words.get("собеседники", []))
            mine = (f for f in phr if f["говорящий"] in custom_items and not _repeat(f, others_words))
            phr = [f | {"кто": transcript.ME} for f in mine]
        result += phr
    result.sort(key=lambda f: f["начало"])
    name = "Собеседник" if sys_ else "Участник"
    numbers: dict[str, int] = {}
    for f in result:
        if "кто" not in f:
            numbers.setdefault(f["говорящий"], len(numbers) + 1)
            f["кто"] = f"{name} {numbers[f['говорящий']]}"
    return result


def folder_for_file(file: pathlib.Path, recordings: pathlib.Path) -> pathlib.Path:
    """The meeting folder for a ready file; a same-named file from another place gets its own numbered folder."""
    source = str(file.resolve())
    folder, number = recordings / file.stem, 2
    while (folder / SOURCE).exists() and (folder / SOURCE).read_text(encoding="utf-8") != source:
        folder, number = recordings / f"{file.stem}_{number}", number + 1
    folder.mkdir(parents=True, exist_ok=True)
    (folder / SOURCE).write_text(source, encoding="utf-8")
    written = datetime.datetime.fromtimestamp(file.stat().st_mtime).isoformat(timespec="seconds")
    catalog.set_(folder, started=written)  # the list then does not look for the file, maybe on a drive that is off
    return folder


def from_file(file: pathlib.Path, recordings: pathlib.Path) -> pathlib.Path:
    """Puts a ready recording (video or audio) into <recordings>/<file name>/общая.flac; the source file stays as is."""
    folder = folder_for_file(file, recordings)
    shared_track = folder / "общая.flac"
    if not shared_track.exists():
        _ffmpeg("-y", "-i", str(file), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "flac", str(shared_track))
    return folder


def _notes(tracks: list[str], levels: dict[str, tuple | None]) -> list[str]:
    if "общая" in tracks:
        return ["Готовая запись одной дорожкой, свои реплики не выделены, все говорящие - участники."]
    if "микрофон" not in tracks:
        return ["Микрофон не записан, свои реплики отмечены как собеседник."]
    if levels.get("собеседники") is None:
        return ["В звуке компьютера нет речи, встреча считается очной: свои реплики не выделены."]
    return []


def _for_track(name: str, levels: dict[str, tuple | None], params_: Params) -> Params:
    """The microphone is you: tell speakers apart on it only when there is no computer audio (an in-person meeting),
    otherwise diarization splits one voice into many."""
    if name == "микрофон" and levels.get("собеседники") is not None:
        return dataclasses.replace(params_, diarization=False)
    return params_


def _readable_parts(parts: object) -> bool:
    return isinstance(parts, list) and all(
        isinstance(part, dict) and isinstance(part.get("сдвиг"), int | float) and providers.readable(part.get("ответ"))
        for part in parts
    )


def _cached(path: pathlib.Path) -> dict:
    """Answers saved earlier; a track whose saved answer cannot be read (an earlier version saved it unchecked) is
    asked again."""
    answers = transcript.read_json(path, {})
    if broken := [name for name, parts in answers.items() if not _readable_parts(parts)]:
        log.warning("saved answers of %s in %s cannot be read, asking again", broken, path)
    return {name: parts for name, parts in answers.items() if name not in broken}


@dataclasses.dataclass
class _Progress:
    """Progress of recognizing a meeting: in the cloud, audio parts by provider answers; on this computer, seconds of
    audio by the engine progress."""

    local_engine: bool
    progress: Callable[[int, int], None] | None
    lengths: dict[str, float]
    done_count: int = 0
    done: int = 0  # on this computer: seconds in the tracks already recognized

    def __post_init__(self) -> None:
        self.total = sum(self._measure(name) for name in self.lengths)

    def _measure(self, name: str) -> int:
        return max(1, round(self.lengths[name])) if self.local_engine else part_count(self.lengths[name])

    def report(self, done_count: int) -> None:
        self.done_count, self.total = done_count, max(self.total, done_count)
        if self.progress:
            self.progress(self.done_count, self.total)

    def part(self) -> None:
        if not self.local_engine:
            self.report(self.done_count + 1)

    def track_progress(self) -> Callable[[int, int], None] | None:
        """Engine progress on a track, in seconds of its audio."""
        return (lambda done_count, _total: self.report(self.done + done_count)) if self.local_engine else None

    def track_done(self, name: str) -> None:
        if self.local_engine:
            self.done += self._measure(name)
            self.report(self.done)


def _recognize_tracks(
    folder: pathlib.Path, need: dict[str, Params], answers: dict, account: _Progress, answered: Callable[[dict], None]
) -> None:
    """The tracks into answers, each saved as soon as it is ready; chunks of a long track are saved one by one into
    PARTS, so "Retry" after a break sends only the rest."""
    partial = {name: parts for name, parts in _cached(folder / PARTS).items() if name in need} if need else {}
    if need:
        account.report(sum(len(partial.get(name, [])) for name in need))

    def save(name: str, parts: list[dict]) -> None:
        partial[name] = parts
        transcript.write_json(folder / PARTS, partial, None)

    for name, params_ in need.items():
        chunks = Chunks(partial.get(name, []), answered, functools.partial(save, name))
        path, length = folder / f"{name}.flac", account.lengths[name]
        answers[name] = recognize_track(path, length, params_, chunks, account.track_progress())
        account.track_done(name)
        transcript.write_json(folder / ANSWERS, answers, None)
        if partial.pop(name, None) is not None:
            transcript.write_json(folder / PARTS, partial, None)
    if not partial:
        (folder / PARTS).unlink(missing_ok=True)


def process(
    folder: pathlib.Path,
    params_: Params,
    again: bool = False,
    progress: Callable[[int, int], None] | None = None,
    payment: Callable[[str, float], None] | None = None,
) -> pathlib.Path:
    """Recognizes the tracks of a folder (takes ready answers from the cache) and writes the transcript.

    progress(done, total) runs after each provider request: how many audio parts are already recognized.
    payment(model, amount) also runs after each request: the spending is recorded even if the next request fails.
    """
    paths = {name: folder / f"{name}.flac" for name in TRACKS if (folder / f"{name}.flac").exists()}
    if not paths:
        raise providers.ProviderError(tr("Folder {folder} has no recorded tracks", folder=folder.name))
    durations, levels = {}, {}
    for name, p in paths.items():
        durations[name], levels[name] = _measure(p)
    if again:
        (folder / PARTS).unlink(missing_ok=True)
    answers = {} if again else _cached(folder / ANSWERS)
    need = {
        name: _for_track(name, levels, params_) for name in paths if name not in answers and levels[name] is not None
    }
    account = _Progress(params_.provider.local_engine, progress, {name: durations[name] for name in need})

    def answered(answer: dict) -> None:
        if payment:
            payment(params_.final_model, providers.cost(answer))
        account.part()

    _recognize_tracks(folder, need, answers, account, answered)
    result = _collect({name: words_from(parts) for name, parts in answers.items()}, levels)
    for phrase in result:
        phrase["текст"] = vocab.fix(phrase["текст"], list(params_.vocabulary))
    info = {
        "длительность": max(durations.values()),
        "стоимость": sum(providers.cost(ch["ответ"]) for parts in answers.values() for ch in parts),
        "провайдер": f"{params_.provider.title}, {params_.final_model}",
    }
    return transcript.save(folder, result, info, _notes(list(paths), levels))


def main(arguments: list[str], payment: Callable[[pathlib.Path], Callable[[str, float], None]] | None = None) -> int:
    """payment(folder) gives where to record the meeting spending."""
    paths = [a for a in arguments if not a.startswith("--")]
    if len(paths) != 1:
        print(__doc__)
        return 2
    config = settings.load()
    path = pathlib.Path(paths[0])
    try:
        folder = from_file(path, config.recordings) if path.is_file() else path
        print(
            process(
                folder,
                params(config),
                "--again" in system.english_flags(arguments),
                payment=payment and payment(folder),
            )
        )
    except providers.ProviderError as error:
        print(error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
