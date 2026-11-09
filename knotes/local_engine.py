# SPDX-License-Identifier: GPL-3.0-only
"""Recognizes a track with the sherpa-onnx engine on this computer, in a separate process (flag --local-engine): jobs
come as JSON lines on stdin, progress and the answers go as JSON lines to stdout, the answer is shaped like the cloud
one (text, duration, words). After the meeting a process gets one job (recognize_in_process); during the meeting one
process takes piece after piece of the recording and keeps the model loaded (Server).

Silero VAD cuts the track into speech chunks up to 20 seconds, the tokens of a chunk are glued into words timed from
the track start. With speaker diarization a word gets the speaker of the segment that overlaps it most, a word outside
all segments gets the nearest one. The program computes the model file paths: the engine process does not scan folders.
"""

import contextlib
import dataclasses
import json
import logging
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from typing import IO

import numpy as np

from knotes import models, providers, system, language
from knotes.language import tr_key, tr

SAMPLE_RATE = 16000
FLAG = "--local-engine"
CHUNK_S = 20  # GigaAM recognizes chunks up to 25 seconds, beyond that quality drops
LIMIT_S = 25  # a longer chunk (speech without pauses) is split into equal parts
MARGIN_S = 0.25  # audio around a speech chunk: without it GigaAM loses the start of the first word
TOKEN_LENGTH = 0.25  # only a token's start is known: its end is no later than this and no later than the next token
# Voice similarity threshold for counting speakers. Picked on two real meetings: 0.5 split one voice into 7,
# 0.9 merged three into one, 0.7-0.8 gave the right count on both
VOICE_THRESHOLD = 0.75
FRAGMENT_SHARE = 0.02  # a "speaker" with less of the speech time is a stray fragment of another voice
WORD_START = (" ", "▁")  # a new word token starts with this: a space in Whisper, "▁" in NeMo models
SILENCE_S = 600  # the engine writes no line for this long: it hung
JOB_SILENCE_S = 180  # the same for a later job of an engine of many jobs: its model is loaded already
RESTART_S = 30.0  # an engine process of many jobs is started again no sooner after its previous start
STOP_WAIT_S = 5.0  # an idle engine exits by the stop line within this, otherwise it is killed
STOP = "стоп"  # the job line {"стоп": true} ends the engine process
CRASH_CODES = (0xC0000005, 0xC0000409)  # Windows: access violation, buffer overrun
OUT_OF_MEMORY = 0xC0000017
NOT_DOWNLOADED = tr_key(
    "Модель «{title}» не скачана. Скачайте ее или выберите другую в настройках, раздел «Распознавание речи»."
)
BROKEN = tr_key("Модель «{title}» повреждена. Скачайте ее заново в настройках, раздел «Распознавание речи».")
NOT_UNDERSTOOD = tr_key("Модель «{title}» не открылась: программа ее не понимает. Выберите другую.")
MEMORY = tr_key(
    "Не хватает памяти для распознавания на этом компьютере. Закройте другие программы или выберите модель поменьше."
)
FAILURE = tr_key("Распознавание на этом компьютере прервалось (код {code}). Подробности в журнале программы.")
CRASH_EXIT = tr_key(
    "Движок аварийно завершился на модели «{title}». Повторите; если повторится, выберите другую модель."
)
FAILED_MODEL = tr_key(
    "Модель «{title}» не справилась с этой записью. Повторите; если повторится, выберите другую модель."
)
HUNG = tr_key(
    "Движок распознавания завис: больше 10 минут ни строки хода. Повторите; если повторится, выберите другую модель."
)
ENGINE_FELL = tr_key(
    "Движок распознавания аварийно завершился на модели «{title}» (код {code}). Если это повторяется, выберите другую "
    "модель."
)
JOB_HUNG = tr_key("Движок распознавания завис и остановлен. Если это повторяется, выберите модель поменьше.")
CANCELLED_TEXT = tr_key("Распознавание на этом компьютере отменено.")
DIARIZATION = tr_key("Разметка говорящих")
log = logging.getLogger(__name__)


class Cancelled(providers.ProviderError):
    """Recognition was stopped: the engine process is terminated."""


@dataclasses.dataclass(frozen=True)
class Model:
    """A downloaded model: its file paths by role (models.ROLES), kind (a key of models.KINDS), the Silero VAD file,
    title and id for error texts and the log, the meeting language for models that ask for it (empty: they detect it
    themselves); custom: not from the catalog."""

    files: dict[str, str]
    kind: str
    vad: str
    title: str
    language: str = ""
    id_: str = ""
    custom: bool = False


@dataclasses.dataclass(frozen=True)
class Diarization:
    """Files of the helper models for speaker diarization: segmentation by voice (pyannote) and the voice embedding."""

    segmented: str
    voice: str


def _path(path: str, title: str) -> str:
    if not path or not os.path.isfile(path):
        raise providers.ProviderError(tr(NOT_DOWNLOADED, title=title))
    return path


def _shortage(error: Exception) -> bool:
    """This is how onnxruntime reports that it could not allocate memory."""
    return isinstance(error, MemoryError) or "alloc" in str(error).lower()


def _open_item(create: Callable[[], object], title: str, custom: bool = False) -> object:
    """Loads a model into the engine; an onnxruntime error becomes a clear text: out of memory, a broken file or (for
    a custom model) the program does not understand it."""
    try:
        return create()
    except (RuntimeError, MemoryError) as error:
        log.warning("model %s did not open: %s", title, error)
        text = tr(MEMORY) if _shortage(error) else tr(NOT_UNDERSTOOD if custom else BROKEN, title=title)
        raise providers.ProviderError(text) from error


def _time_by_segments(tokens: list[str], segments: list[tuple[float, float, str]]) -> list[tuple[float, float]]:
    """Times of tokens without their own timestamps: inside a segment (start, length, text) by the share of letters."""
    boundaries = np.cumsum([0] + [len(text) for _, _, text in segments])
    positions = np.cumsum([0] + [len(token) for token in tokens]) * boundaries[-1] / max(1, sum(map(len, tokens)))

    def time(position: float, side: str) -> float:
        number = min(max(int(np.searchsorted(boundaries, position, side)) - 1, 0), len(segments) - 1)
        start, length, _ = segments[number]
        letters = max(1, boundaries[number + 1] - boundaries[number])
        return float(start + length * min(1.0, (position - boundaries[number]) / letters))

    return [(time(a, "right"), time(b, "left")) for a, b in zip(positions, positions[1:], strict=False)]


def token_times(result: object, length: float) -> list[tuple[float, float]]:
    """Start and end of each token of a sherpa-onnx result in seconds from the start of a chunk of the given length."""
    tokens, starts = list(result.tokens), list(result.timestamps)
    if tokens and len(starts) == len(tokens):
        durations = list(result.durations)
        if len(durations) == len(tokens):
            return [(n, n + d) for n, d in zip(starts, durations, strict=True)]
        return [(n, min(n + TOKEN_LENGTH, next_)) for n, next_ in zip(starts, starts[1:] + [length], strict=True)]
    segments = list(zip(result.segment_timestamps, result.segment_durations, result.segment_texts, strict=False))
    return _time_by_segments(tokens, segments or [(0.0, length, "".join(tokens))])


def words_from_tokens(tokens: Iterable[tuple[str, float, float]], shift: float = 0.0) -> list[dict]:
    """Tokens (text, start, end) of one chunk into words timed from the track start.

    A new word starts with a token preceded by a space or "▁", and at the chunk start. A token of a single space only
    marks the start of a word, a punctuation mark without a space sticks to the word before it.
    """
    words: list[dict] = []
    new_value = True
    for text, start, end in tokens:
        new_value = new_value or text.startswith(WORD_START)
        text = text.replace("▁", " ").strip()
        if not text:
            continue
        if new_value:
            words.append({"start": start + shift, "end": end + shift, "word": text})
        else:
            words[-1]["end"], words[-1]["word"] = end + shift, words[-1]["word"] + text
        new_value = False
    return words


def _without_fragments(segments: list[tuple[float, float, int]]) -> list[tuple[float, float, int]]:
    """Segments of speakers with at least FRAGMENT_SHARE of the speech; words of the dropped ones go to the
    nearest remaining speaker."""
    speech: dict[int, float] = {}
    for start, end, who in segments:
        speech[who] = speech.get(who, 0.0) + end - start
    total = sum(speech.values()) or 1.0
    stay = [o for o in segments if speech[o[2]] / total >= FRAGMENT_SHARE]
    return stay or segments


def speakers(words: list[dict], segments: list[tuple[float, float, int]]) -> list[str]:
    """The speaker of each word: the diarization segment that overlaps the word more than others; if none overlaps,
    the nearest one. Numbers follow the order of appearance in the diarization: speaker_0, speaker_1..."""
    if not segments:
        return ["speaker_0"] * len(words)
    segments = _without_fragments(sorted(segments))
    numbers: dict[int, int] = {}
    for *_, who in segments:
        numbers.setdefault(who, len(numbers))
    starts, ends = np.array([o[0] for o in segments]), np.array([o[1] for o in segments])
    result = []
    for word in words:
        # an overlapping segment gives its overlap length, a segment off to the side gives minus its distance
        overlap = np.minimum(ends, word["end"]) - np.maximum(starts, word["start"])
        result.append(f"speaker_{numbers[segments[int(np.argmax(overlap))][2]]}")
    return result


def _split(start: float, chunk: np.ndarray) -> Iterator[tuple[float, np.ndarray]]:
    """Splits a speech chunk without pauses longer than LIMIT_S into equal parts up to CHUNK_S."""
    if len(chunk) <= LIMIT_S * SAMPLE_RATE:
        yield start, chunk
        return
    for part in np.array_split(chunk, int(np.ceil(len(chunk) / (CHUNK_S * SAMPLE_RATE)))):
        yield start, part
        start += len(part) / SAMPLE_RATE


def _diarization_settings(so: object, diarization: Diarization, thread_count: int) -> object:
    segmented, voice = (_path(path, tr(DIARIZATION)) for path in (diarization.segmented, diarization.voice))
    return so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=segmented), num_threads=thread_count
        ),
        embedding=so.SpeakerEmbeddingExtractorConfig(model=voice, num_threads=thread_count),
        clustering=so.FastClusteringConfig(num_clusters=-1, threshold=VOICE_THRESHOLD),
    )


def _sherpa() -> object:
    import sherpa_onnx  # here, not at the top: onnxruntime loads only in the engine process

    return sherpa_onnx


class Engine:
    """sherpa-onnx for one model: speech cutting, chunk recognition, speaker diarization of the whole track."""

    def __init__(self, model: Model, diarization: Diarization | None, so: object = None) -> None:
        self.so = so = so or _sherpa()
        thread_count = max(1, (os.cpu_count() or 2) // 2)  # half of the cores: the computer does not freeze
        kind = models.KINDS[model.kind]
        files = {param: _path(model.files.get(role, ""), model.title) for param, role in kind.files.items()}
        files |= {"language": model.language} if kind.with_language else {}
        self.vad = so.VadModelConfig(
            silero_vad=so.SileroVadModelConfig(model=_path(model.vad, model.title), max_speech_duration=CHUNK_S),
            sample_rate=SAMPLE_RATE,
        )
        config = diarization and _diarization_settings(so, diarization, thread_count)  # all files before the long load
        _open_item(lambda: so.VoiceActivityDetector(self.vad), model.title)  # a broken VAD fails now, not on audio
        open_view = getattr(so.OfflineRecognizer, kind.method)
        self.recognizer = _open_item(
            lambda: open_view(**files, **kind.other, num_threads=thread_count), model.title, model.custom
        )
        self.diarizer = (
            _open_item(lambda: so.OfflineSpeakerDiarization(config), tr(DIARIZATION)) if diarization else None
        )

    def _speech(self, audio: np.ndarray) -> Iterator[tuple[int, int]]:
        """Speech segments (start, length) in samples as they become ready."""
        vad = self.so.VoiceActivityDetector(self.vad)
        window = self.vad.silero_vad.window_size  # with larger blocks VAD does not cut speech at the length limit
        for start in range(0, len(audio), window):
            vad.accept_waveform(audio[start : start + window])
            yield from self._ready_items(vad)
        vad.flush()
        yield from self._ready_items(vad)

    @staticmethod
    def _ready_items(vad: object) -> Iterator[tuple[int, int]]:
        while not vad.empty():
            segment = vad.front  # a reference into the VAD: after pop() its place holds garbage
            start, length = segment.start, len(segment.samples)
            vad.pop()
            yield start, length

    def cut(self, audio: np.ndarray) -> Iterator[tuple[float, np.ndarray]]:
        """Speech chunks (start in seconds, audio) with a margin around them: VAD notices speech slightly after it
        starts. The margin does not reach into the previous chunk, so words are not repeated."""
        before, margin = 0, int(MARGIN_S * SAMPLE_RATE)
        for start, length in self._speech(audio):
            s, by = max(start - margin, before), min(start + length + margin, len(audio))
            before = by
            yield from _split(s / SAMPLE_RATE, audio[s:by])

    def recognize(self, chunk: np.ndarray) -> tuple[str, list[tuple[str, float, float]]]:
        """Text of a chunk and its tokens (text, start, end) timed from the chunk start."""
        stream = self.recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, chunk)
        self.recognizer.decode_stream(stream)
        result = stream.result
        times = token_times(result, len(chunk) / SAMPLE_RATE)
        return result.text, [(token, *v) for token, v in zip(result.tokens, times, strict=False)]

    def diarize(self, audio: np.ndarray, progress: Callable[[int, int], None]) -> list[tuple[float, float, int]]:
        """Track segments (start, end, speaker number); progress(done, total) counts diarization chunks."""
        result = self.diarizer.process(audio, callback=lambda done, total: progress(done, total) or 0)
        return [(o.start, o.end, o.speaker) for o in result.sort_by_start_time()]


def recognize(
    audio: np.ndarray,
    model: Model,
    diarization: Diarization | None,
    progress: Callable[[int, int], None] | None = None,
    engine: object = None,
) -> dict:
    """A track (mono 16 kHz float32) into an answer shaped like the cloud one; diarization None means one speaker.

    progress(done, total) is in seconds of audio; with diarization the first half of the progress is recognition, the
    second is diarization. engine is for tests; by default Engine(model, diarization).
    """
    engine = engine or Engine(model, diarization)
    duration = len(audio) / SAMPLE_RATE
    total, share, before = max(1, round(duration)), 0.5 if diarization else 1.0, -1

    def mark(part: float) -> None:
        nonlocal before
        done_count = min(total, round(part * total))
        if progress and done_count != before:
            before = done_count
            progress(done_count, total)

    mark(0)
    words, texts = [], []
    for start, chunk in engine.cut(audio):
        text, tokens = engine.recognize(chunk)
        texts.append(text.strip())
        words += words_from_tokens(tokens, start)
        mark(share * (start + len(chunk) / SAMPLE_RATE) / max(duration, 1e-9))
    segments = []
    if diarization:
        segments = engine.diarize(audio, lambda done, from_: mark(share + (1 - share) * done / max(from_, 1)))
    for word, who in zip(words, speakers(words, segments), strict=True):
        word |= {"start": round(word["start"], 3), "end": round(word["end"], 3), "speaker": who}
    mark(1)
    return {"text": " ".join(t_ for t_ in texts if t_), "duration": duration, "words": words}


def _task_input(path: os.PathLike | str, model: Model, diarization: Diarization | None) -> dict:
    return {
        "звук": str(path),
        "модель": dataclasses.asdict(model),
        "разметка": diarization and dataclasses.asdict(diarization),
        "язык": language.current(),
    }


def _from_job(job: dict) -> tuple[str, Model, Diarization | None]:
    diarization = job.get("разметка")
    return str(job["звук"]), Model(**job["модель"]), diarization and Diarization(**diarization)


def _read(path: str) -> np.ndarray:
    from knotes import recognition  # here, not at the top: recognition itself imports this module

    return recognition.read_mono(pathlib.Path(path))


def _channel() -> IO[str]:
    """The output for talking to the program is a copy of stdout, and stdout itself goes to stderr: what native code
    prints does not mix with the JSON lines."""
    copy_ = os.dup(1)
    os.dup2(2, 1)
    if sys.stdout:  # it writes to stderr now, so in UTF-8 like the log, not in the Windows code page
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return open(copy_, "w", encoding="ascii", errors="replace")  # noqa: SIM115 - lives until the process ends


class _Loaded:
    """The engine of the last job: the next job with the same model and diarization does not load them again."""

    def __init__(self) -> None:
        self.key: tuple[Model, Diarization | None] | None = None
        self.engine: Engine | None = None

    def get(self, model: Model, diarization: Diarization | None) -> Engine:
        if self.engine is None or self.key != (model, diarization):
            self.engine = None  # the old model leaves memory before the new one loads
            self.engine, self.key = Engine(model, diarization), (model, diarization)
        return self.engine


def _run(job: dict, write_text: Callable[[dict], None], loaded: _Loaded) -> int:
    try:
        path, model, diarization = _from_job(job)
        audio, engine = _read(path), loaded.get(model, diarization)
        answer = recognize(audio, model, diarization, lambda s, from_: write_text({"ход": [s, from_]}), engine)
    except providers.ProviderError as error:
        write_text({"ошибка": str(error)})
        return 1
    except Exception as error:  # noqa: BLE001 - top of the engine process: any failure goes to the log as a clear text
        if _shortage(error):
            log.warning("out of memory: %s", error)
            write_text({"ошибка": tr(MEMORY)})
        else:
            log.exception("engine crashed")
            write_text({"ошибка": tr(FAILURE, code=type(error).__name__)})
        return 1
    write_text({"ответ": answer})
    return 0


def main(arguments: list[str], stdin: IO[str] | None = None, output: IO[str] | None = None) -> int:
    """Child process: jobs come on stdin one JSON line each, progress and the answers go as JSON lines to stdout; the
    model of a job stays loaded for the next jobs with the same model. The process exits with the code of the last
    job when stdin ends (the program closed it, exited or died) or on the line {"стоп": true}. The log goes to stderr.

    Lines: {"ход": [done, total]}, at the end of each job {"ответ": {...}} or {"ошибка": "text for the user"}.
    """
    del arguments  # the program has already parsed the flag
    if output is None:
        if sys.stderr:  # the program reads failure messages in UTF-8
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
        output = _channel()

    def write_text(recording: dict) -> None:
        try:
            output.write(json.dumps(recording) + "\n")  # ASCII only: the channel encoding does not matter
            output.flush()
        except OSError:  # the program is gone: nobody reads the answer, the rest of the job is not needed
            os._exit(0)

    code, loaded = 0, _Loaded()
    for line in stdin or sys.stdin:
        job = json.loads(line) if line.strip() else {}
        if job.get(STOP):
            break
        if job:
            language.set_(job.get("язык", "ru"))
            code = _run(job, write_text, loaded)
    return code


def parse(lines: Iterable[bytes | str], progress: Callable[[int, int], None] | None = None) -> dict | None:
    """Lines of the child process: progress goes to progress(), the answer is the result, an error raises
    ProviderError; None: no answer. Lines that are not JSON or not dicts are skipped."""
    for line in lines:
        try:
            recording = json.loads(line)
        except ValueError:
            recording = None
        if not isinstance(recording, dict):
            log.warning("engine printed something unexpected: %r", line[:200])
            continue
        if "ход" in recording and progress:
            progress(*recording["ход"])
        elif "ошибка" in recording:
            raise providers.ProviderError(recording["ошибка"])
        elif "ответ" in recording:
            return recording["ответ"]
    return None


def _flags() -> int:
    """A process without a console window and with lower priority: recording and windows stay smooth during
    recognition."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)


@dataclasses.dataclass
class _Watchdog:
    """Watches the engine process during a job: cancelling terminates it, and so does silence longer than silence
    seconds (it hung). Watching ends with the process or when the job is done."""

    process_: subprocess.Popen
    cancel: threading.Event
    silence: float
    last: float = dataclasses.field(default_factory=time.monotonic)
    hung: bool = False
    seconds: int = 0  # audio duration from the engine progress, for the log
    done: threading.Event = dataclasses.field(default_factory=threading.Event)

    def lines(self, stream: Iterable[bytes]) -> Iterator[bytes]:
        for line in stream:
            self.last = time.monotonic()
            yield line

    def progress(self, progress: Callable[[int, int], None] | None) -> Callable[[int, int], None]:
        def accept(done_count: int, total: int) -> None:
            self.seconds = total
            if progress:
                progress(done_count, total)

        return accept

    def watch(self) -> None:
        while not self.cancel.wait(0.2):
            if self.done.is_set() or self.process_.poll() is not None:
                return
            if time.monotonic() - self.last > self.silence:
                self.hung = True
                break
        if not self.done.is_set():  # cancelled after the answer: the idle process exits by the stop line
            self.process_.kill()


def _start(messages: IO[bytes]) -> subprocess.Popen:
    """The engine process; its messages (stderr) go to messages."""
    command, extra_env = system.startup(FLAG, console=True)
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=messages,
        env=os.environ | extra_env,
        creationflags=_flags(),
    )


def _send(process_: subprocess.Popen, job: dict, more: bool = False) -> None:
    """A job line to the engine; without more jobs its input is closed."""
    try:
        process_.stdin.write(json.dumps(job).encode() + b"\n")
        if more:
            process_.stdin.flush()
        else:
            process_.stdin.close()
    except OSError:  # the process has already exited: the reason will be in the exit code and messages
        log.warning("engine did not accept the job")


def _end(process_: subprocess.Popen) -> None:
    """Kills the engine process if it still runs, waits for it and closes its pipes."""
    if process_.poll() is None:
        process_.kill()
    process_.wait()
    for pipe in (process_.stdin, process_.stdout):
        with contextlib.suppress(OSError):  # a job line that did not get through stays in the buffer
            pipe.close()


def _tail(messages: IO[bytes]) -> str:
    """The end of the engine messages for the log."""
    messages.seek(0, os.SEEK_END)
    messages.seek(max(0, messages.tell() - 4000))
    return messages.read().decode("utf-8", "replace")


def _log_exit(code: int, watchdog: _Watchdog, job: tuple[Model, Diarization | None], messages: str) -> None:
    model, diarization = job
    log.warning(
        "engine exited with code 0x%X (%s): model %s (%s), audio %d s, diarization %s: %s",
        code & 0xFFFFFFFF,
        "hung" if watchdog.hung else "by itself",
        model.id_,
        model.kind,
        watchdog.seconds,
        "yes" if diarization else "no",
        messages,
    )


def _crash(code: int, model: Model) -> str:
    """Why the engine process exited without an answer, by the exit code."""
    code &= 0xFFFFFFFF
    if code in CRASH_CODES:
        return tr(CRASH_EXIT, title=model.title)
    if code == OUT_OF_MEMORY:
        return tr(MEMORY)
    if code == 1:
        return tr(FAILED_MODEL, title=model.title)
    return tr(FAILURE, code=code)


def _fell(code: int, model: Model) -> str:
    """Why the engine process of many jobs exited without an answer: the next job starts it again."""
    code &= 0xFFFFFFFF
    return tr(MEMORY) if code == OUT_OF_MEMORY else tr(ENGINE_FELL, title=model.title, code=f"0x{code:X}")


def recognize_in_process(
    path: os.PathLike | str,
    model: Model,
    diarization: Diarization | None,
    progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
) -> dict:
    """The same as recognize(), for a recording file, in a separate program process.

    cancel.set() from another thread terminates the process, then Cancelled is raised. On failure the engine messages
    (stderr) go to the log.
    """
    with tempfile.TemporaryFile() as messages:
        process_ = _start(messages)
        watchdog, answer = _Watchdog(process_, cancel or threading.Event(), SILENCE_S), None
        threading.Thread(target=watchdog.watch, daemon=True).start()
        try:
            _send(process_, _task_input(path, model, diarization))
            answer = parse(watchdog.lines(process_.stdout), watchdog.progress(progress))
        finally:
            _end(process_)
            if answer is None:  # engine error, crash, cancel: its messages go to the log
                _log_exit(process_.returncode, watchdog, (model, diarization), _tail(messages))
    if watchdog.cancel.is_set():
        raise Cancelled(tr(CANCELLED_TEXT))
    if answer is None:
        raise providers.ProviderError(tr(HUNG) if watchdog.hung else _crash(process_.returncode, model))
    return answer


class Server:
    """One engine process for many jobs in a row, for the transcript during a meeting: the model loads with the first
    job and stays loaded for the next ones. Jobs and close() come from one thread at a time, one job at a time. A
    process that crashed or hung is started again by the next job, no sooner than RESTART_S after its previous start."""

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._messages: IO[bytes] | None = None  # stderr of the process
        self._started = float("-inf")  # monotonic time of the last start
        self._loaded = False  # the process has answered: its model is loaded, a job waits for less

    def recognize(
        self,
        path: os.PathLike | str,
        model: Model,
        diarization: Diarization | None,
        progress: Callable[[int, int], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> dict:
        """The same as recognize_in_process(), in the process of this server. An error of the engine keeps the
        process; cancel.set() from another thread kills it, then Cancelled is raised."""
        cancel = cancel or threading.Event()
        process_ = self._running(cancel)
        watchdog = _Watchdog(process_, cancel, JOB_SILENCE_S if self._loaded else SILENCE_S)
        threading.Thread(target=watchdog.watch, daemon=True).start()
        try:
            _send(process_, _task_input(path, model, diarization), more=True)
            answer = parse(watchdog.lines(process_.stdout), watchdog.progress(progress))
        finally:
            watchdog.done.set()
        if answer is not None:
            self._loaded = True
            return answer
        code, messages = self._release()  # it crashed, hung or was cancelled
        _log_exit(code, watchdog, (model, diarization), messages)
        if cancel.is_set():
            raise Cancelled(tr(CANCELLED_TEXT))
        raise providers.ProviderError(tr(JOB_HUNG) if watchdog.hung else _fell(code, model))

    def _running(self, cancel: threading.Event) -> subprocess.Popen:
        """The engine process; a new one waits until RESTART_S have passed since the previous start."""
        if self._process is not None and self._process.poll() is None:
            return self._process
        if self._process is not None:
            code, messages = self._release()
            log.warning("engine exited between jobs with code 0x%X: %s", code & 0xFFFFFFFF, messages)
        if cancel.wait(max(0.0, self._started + RESTART_S - time.monotonic())):
            raise Cancelled(tr(CANCELLED_TEXT))
        self._messages = tempfile.TemporaryFile()  # noqa: SIM115 - lives as long as the process
        self._process, self._started, self._loaded = _start(self._messages), time.monotonic(), False
        return self._process

    def _release(self) -> tuple[int, str]:
        """Ends the process and lets it go: its exit code and the end of its messages."""
        process_, messages = self._process, self._messages
        self._process = self._messages = None
        _end(process_)
        with messages:
            return process_.returncode, _tail(messages)

    def close(self) -> None:
        """Ends the engine process: an idle one exits by the stop line, one that does not in STOP_WAIT_S is killed."""
        if self._process is None:
            return
        if self._process.poll() is None:
            _send(self._process, {STOP: True})
            try:
                self._process.wait(STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                log.warning("engine did not exit by the stop line in %s s", STOP_WAIT_S)
        self._release()
