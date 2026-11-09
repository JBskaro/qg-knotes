# SPDX-License-Identifier: GPL-3.0-only
"""Transcript during the meeting (a setting, off by default): while recording, new audio is recognized in the
background by the model on this computer, and the analyst can ask the minutes model about the part of the meeting
already recognized ("what did the client promise to send?").

The recording files are only read. A piece is the audio of both tracks from the previous cut to now, read from the
chunks listed in запись.json (a chunk file grows while it is recorded); it ends at the quietest moment near its end,
so a word is not split between pieces. The tracks of a piece go to the engine as one file (the microphone, a pause,
the computer audio), without speaker diarization. One engine process of lower priority lives through the recording
(local_engine.Server): the model loads with the first piece and stays loaded, the process ends with the recording.
Pieces go one after another: the next waits until the previous one is recognized. Echo of the speakers in the
microphone is dropped as after the meeting. The final transcript after the meeting is made as usual; a failure here is
logged and shown, the recording goes on; a crashed or hung engine is started again with a later piece.
"""

import dataclasses
import functools
import html
import logging
import pathlib
import re
import tempfile
import threading
import time
import wave
from collections.abc import Callable

import numpy as np

from knotes import catalog, failures, local_engine, minutes, providers, recognition, spending, transcript
from knotes import settings
from knotes import vocabulary as vocab
from knotes.language import tr, tr_key

SAMPLE_RATE = recognition.SAMPLE_RATE
TRACKS = ("микрофон", "собеседники")  # recording tracks, as recording.py names their chunks
PERIOD_S = 25.0  # how often new audio is taken
REST_S = 5.0  # pause after a piece that took longer than PERIOD_S: the recording and the computer go first
MIN_PIECE_S = 8.0  # less new audio waits for the next round
MAX_PIECE_S = 120.0  # catching up after a slow piece: in pieces no longer than this
BEHIND_S = 1.0  # the last second may not be on disk yet
CUT_SEARCH_S = 3.0  # a piece ends at the quietest moment of its last seconds
GAP_S = 2.0  # silence between the tracks in the engine file: speech cutting never joins them
PAUSE_S = 2.0  # a longer pause starts a new phrase, as in recognition.phrases()
MAX_FAILURES = 3  # failures in a row: the transcript during this meeting stops
QUESTION_CHARS = 60_000  # the latest part of a long meeting goes to the model
HEADER_BYTES = 256
ME, OTHERS, EVERYONE = tr_key("Я"), tr_key("Собеседники"), tr_key("Участники")  # data labels, shown via tr()
HINT = tr_key(
    "Пока идет запись, речь распознается каждые полминуты: в блокноте видны последние фразы, и модели протокола "
    "можно задать вопрос по уже сказанному. Работает с распознаванием на этом компьютере и нагружает процессор. "
    "Итоговая расшифровка после встречи делается как обычно."
)
CLOUD = tr_key(
    "Недоступно при облачном распознавании: запись пришлось бы оплачивать дважды. Выберите распознавание на этом "
    "компьютере."
)
PROMPT = (
    "Идет встреча. Ниже - расшифровка ее уже записанной части, распознанная на лету: в словах бывают ошибки "
    "распознавания. «Я» - владелец программы, аналитик, «Собеседники» - остальные участники, «Участники» - все, "
    "кого слышно в микрофон на очной встрече. Ответь на вопрос аналитика коротко и только по расшифровке, на языке "
    "вопроса. После каждого факта поставь минуту записи, где он прозвучал, в квадратных скобках, как в "
    "расшифровке: [12:05]. Если в расшифровке ответа нет, так и скажи."
)
TIME_MARK = re.compile(rf"\[{transcript.TIME}\]")
log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Phrase:
    start: float  # seconds of the recording
    end: float
    track: str  # "микрофон" or "собеседники"
    text: str


def who(phrase: Phrase, others_heard: bool) -> str:
    """The data label of a phrase. The microphone is "me" only when the computer audio has speech: without it the
    meeting is in person and everyone speaks into the microphone."""
    if phrase.track == "собеседники":
        return OTHERS
    return ME if others_heard else EVERYONE


def note_text(phrase: Phrase, others_heard: bool) -> str:
    """A phrase as a line of the notes: "Others: we will send the estimate"."""
    return f"{tr(who(phrase, others_heard))}: {phrase.text}"


def display_line(phrase: Phrase, others_heard: bool) -> str:
    return f"{transcript.time(phrase.start)}  {note_text(phrase, others_heard)}"


def nearest(phrases: list[Phrase], second: float) -> int | None:
    """The phrase that starts closest to this second of the recording."""
    if not phrases:
        return None
    return min(range(len(phrases)), key=lambda number: abs(phrases[number].start - second))


def answer_html(text: str, link_color: str = "") -> str:
    """The model's answer for a label: the recording minutes become links to their phrases."""
    look = f' style="color: {link_color}"' if link_color else ""
    escaped = html.escape(text.strip())
    linked = TIME_MARK.sub(lambda m: f'<a href="{transcript.seconds_list(m)}"{look}>{m.group(0)}</a>', escaped)
    return linked.replace("\n", "<br>")


# --- audio of a recording in progress -------------------------------------------------------------------------------


def _chunks(folder: pathlib.Path) -> list[tuple[str, pathlib.Path, float]]:
    """Chunks of the recording from its manifest: (track, file, start in the recording). Only files of this folder
    named as the recording names them: a foreign manifest cannot lead the reading elsewhere."""
    result = []
    for entry in transcript.read_json(folder / transcript.MANIFEST, []):
        if not isinstance(entry, dict):
            continue
        track, file, start = entry.get("дорожка"), str(entry.get("файл", "")), entry.get("начало")
        if track in TRACKS and isinstance(start, int | float) and re.fullmatch(rf"{track}_\d{{2,}}\.wav", file):
            result.append((track, folder / file, float(start)))
    return result


def _wav_format(head: bytes) -> tuple[int, int] | None:
    """Sample rate and where the samples start in a 16-bit mono WAV; None while the header is not written yet. The
    sizes in the header are not used: they lag behind while the file is recorded."""
    if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    place, rate = 12, 0
    while place + 8 <= len(head):
        kind, size = head[place : place + 4], int.from_bytes(head[place + 4 : place + 8], "little")
        if kind == b"fmt " and int.from_bytes(head[place + 20 : place + 22], "little") == 2:  # block align
            rate = int.from_bytes(head[place + 12 : place + 16], "little")
        elif kind == b"data":
            return (rate, place + 8) if rate else None
        place += 8 + size + size % 2
    return None


@functools.cache
def _low_pass(rate: int) -> np.ndarray:
    """Keeps speech up to 7 kHz: without it the higher sounds would fold into speech at 16 kHz."""
    taps = np.arange(-32, 33)
    cutoff = 7000 / rate
    kernel = 2 * cutoff * np.sinc(2 * cutoff * taps) * np.hamming(len(taps))
    return (kernel / kernel.sum()).astype(np.float32)


def to_16k(samples: np.ndarray, rate: int) -> np.ndarray:
    """Mono float32 audio of any sample rate at 16 kHz, as the engine takes it."""
    if rate == SAMPLE_RATE or not samples.size:
        return samples
    if rate > SAMPLE_RATE:
        samples = np.convolve(samples, _low_pass(rate), mode="same")
    positions = np.arange(int(len(samples) * SAMPLE_RATE / rate)) * (rate / SAMPLE_RATE)
    return np.interp(positions, np.arange(len(samples)), samples).astype(np.float32)


def read_wav(path: pathlib.Path, from_s: float, until_s: float) -> np.ndarray:
    """16 kHz audio of a chunk from from_s to until_s seconds of it: only what is on disk, the file is not changed."""
    from_s = max(from_s, 0.0)
    if until_s <= from_s:
        return np.zeros(0, np.float32)
    try:
        with path.open("rb") as file:
            format_ = _wav_format(file.read(HEADER_BYTES))
            if format_ is None:
                return np.zeros(0, np.float32)
            rate, data_at = format_
            first, last = int(from_s * rate), int(until_s * rate)
            file.seek(data_at + 2 * first)
            raw = file.read(2 * (last - first))
    except OSError as error:  # the chunk is gone (the recording was assembled) or busy: no audio this time
        log.warning("live transcript: %s not read: %s", path.name, error)
        return np.zeros(0, np.float32)
    samples = np.frombuffer(raw[: len(raw) // 2 * 2], np.int16).astype(np.float32) / 32768
    return to_16k(samples, rate)


def track_audio(chunks: list[tuple[str, pathlib.Path, float]], track: str, start: float, end: float) -> np.ndarray:
    """A track from start to end of the recording at 16 kHz; where no chunk has audio (a device change), silence."""
    result = np.zeros(int(round((end - start) * SAMPLE_RATE)), np.float32)
    for name, path, began in chunks:
        if name != track:
            continue
        samples = read_wav(path, start - began, end - began)
        at = int(round(max(began - start, 0.0) * SAMPLE_RATE))
        take = min(len(samples), len(result) - at)
        if take > 0:
            result[at : at + take] = samples[:take]
    return result


def quiet_cut(tracks: list[np.ndarray]) -> int:
    """Where a piece ends: the middle of the quietest 50 ms of all tracks in the last CUT_SEARCH_S, the latest of
    equally quiet ones."""
    length, window = len(tracks[0]), SAMPLE_RATE // 20
    begin = max(0, length - int(CUT_SEARCH_S * SAMPLE_RATE))
    count = (length - begin) // window
    if not count:
        return length
    energy = sum((a[begin : begin + count * window] ** 2).reshape(count, window).mean(axis=1) for a in tracks)
    quietest = count - 1 - int(np.argmin(energy[::-1]))
    return begin + quietest * window + window // 2


# --- a piece: one engine run, phrases ------------------------------------------------------------------------------


def engine_audio(audio: dict[str, np.ndarray]) -> tuple[np.ndarray, list[tuple[str, float]]]:
    """The tracks with sound one after another with GAP_S of silence: one engine run for the piece. Returns the audio
    and where each track starts in it, in seconds."""
    parts: list[np.ndarray] = []
    places: list[tuple[str, float]] = []
    at = 0.0
    for track, samples in audio.items():
        if recognition.loudness(samples) is None:
            continue
        if parts:
            parts.append(np.zeros(int(GAP_S * SAMPLE_RATE), np.float32))
            at += GAP_S
        places.append((track, at))
        parts.append(samples)
        at += len(samples) / SAMPLE_RATE
    return (np.concatenate(parts) if parts else np.zeros(0, np.float32)), places


def split_words(words: list[dict], places: list[tuple[str, float]]) -> dict[str, list[dict]]:
    """Words of the joined audio back to their tracks, timed from the piece start."""
    result: dict[str, list[dict]] = {track: [] for track, _ in places}
    for word in words:
        track, at = next(((t, a) for t, a in reversed(places) if word["начало"] >= a - GAP_S / 2), places[0])
        result[track].append(word | {"начало": word["начало"] - at, "конец": word["конец"] - at})
    return result


def _by_phrase(words: list[dict]) -> list[dict]:
    """Each microphone phrase as its own "speaker": whether it is my voice or the echo of the speakers is decided per
    phrase, a piece is too short to judge a voice by."""
    result, number, before = [], 0, None
    for word in words:
        if before is not None and word["начало"] - before > PAUSE_S:
            number += 1
        result.append(word | {"говорящий": f"ф{number}"})
        before = word["конец"]
    return result


def piece_phrases(words: dict[str, list[dict]], audio: dict[str, np.ndarray], start: float) -> list[Phrase]:
    """Phrases of a piece in order of time; microphone phrases that are echo of the computer audio are dropped as in
    the transcript after the meeting."""
    labeled = {track: _by_phrase(w) if track == "микрофон" else w for track, w in words.items()}
    return [
        Phrase(start + f["начало"], start + f["конец"], f["говорящий"].partition("/")[0], f["текст"])
        for f in recognition.collect_phrases(labeled, audio)
    ]


def _write_wav(path: pathlib.Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(SAMPLE_RATE)
        file.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())


# --- a question to the minutes model ------------------------------------------------------------------------------


def _check(params_: minutes.Params) -> None:
    """The same refusals as for the minutes: the question goes to their service and model."""
    title = params_.service.title
    if params_.service.key_needed and not params_.key:
        problem = tr("No API key for {service}, set it in the Minutes section in Settings", service=title)
    elif not (params_.service.address and params_.model):
        problem = tr("No address or model set for {service}, check the Minutes section in Settings", service=title)
    else:
        return
    raise providers.ProviderError(problem)


def _latest(lines: list[str]) -> str:
    """The latest lines that fit into QUESTION_CHARS."""
    kept, size = [], 0
    for line in reversed(lines):
        size += len(line) + 1
        if size > QUESTION_CHARS:
            break
        kept.append(line)
    return "\n".join(reversed(kept))


def question_body(phrases: list[Phrase], others_heard: bool, question: str, params_: minutes.Params) -> dict:
    lines = [f"[{transcript.time(p.start)}] {who(p, others_heard)}: {p.text}" for p in phrases]
    owner = [f"Имя владельца - {params_.my_name}", ""] if params_.my_name else []
    body: dict = {
        "model": params_.model,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": "\n".join([*owner, "Расшифровка", _latest(lines), "", f"Вопрос - {question}"])},
        ],
    }
    if params_.service.kind == "openrouter":
        body["provider"] = {"data_collection": "allow" if params_.retention else "deny"}
    return body


# --- the transcript of one recording ---------------------------------------------------------------------------


class Live:
    """The transcript during one recording: a background thread takes a piece every PERIOD_S. Phrases, the error text
    and whether the computer audio has speech are read by the notepad; version grows with each new phrase."""

    def __init__(
        self,
        folder: pathlib.Path,
        params_: recognition.Params,
        clock: Callable[[], float],
        config: settings.Settings,
    ):
        """clock gives how many seconds are recorded (without pauses); config gives the minutes service and model."""
        self.folder, self.clock, self.config = folder, clock, config
        self.params = dataclasses.replace(
            params_, diarization=False, cancel=threading.Event(), engine=local_engine.Server()
        )
        self.done_until = 0.0  # seconds of the recording already recognized
        self.others_heard = False
        self.error = ""
        self.version = 0
        self._failures = 0
        self._phrases: list[Phrase] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="расшифровка во время встречи", daemon=True)

    def start(self) -> None:
        log.info("live transcript of %s on", self.folder.name)
        self._thread.start()

    def stop(self) -> None:
        """Stops at once: the engine process recognizing a piece is killed, an idle one exits by itself. Does not wait
        for the thread."""
        self.params.cancel.set()
        if not self._thread.is_alive():  # otherwise the thread ends the engine on its way out
            self.params.engine.close()

    def wait(self, seconds: float) -> None:
        self._thread.join(seconds)

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def phrases(self) -> list[Phrase]:
        with self._lock:
            return list(self._phrases)

    def status(self) -> str:
        if self.error:
            return self.error
        if not self._phrases:
            return tr("Phrases appear here about half a minute after they are said")
        return tr("Recognized up to {time}", time=transcript.time(self.done_until))

    def _run(self) -> None:
        pause = PERIOD_S
        try:
            while not self.params.cancel.wait(pause):
                started = time.monotonic()
                self.step()
                pause = max(REST_S, PERIOD_S - (time.monotonic() - started))
        finally:
            self.params.engine.close()

    def _next_piece(self) -> tuple[float, dict[str, np.ndarray]] | None:
        """The new audio of each track, ending at a quiet moment; None while there is too little of it."""
        now = self.clock() - BEHIND_S
        chunks = _chunks(self.folder)
        if now - self.done_until < MIN_PIECE_S or not chunks:
            return None
        start, end = self.done_until, min(now, self.done_until + MAX_PIECE_S)
        recorded = {name for name, *_ in chunks}
        audio = {track: track_audio(chunks, track, start, end) for track in TRACKS if track in recorded}
        cut = quiet_cut(list(audio.values()))
        return start, {track: samples[:cut] for track, samples in audio.items()}

    def step(self) -> bool:
        """Recognizes the next piece if there is enough new audio; False if there was nothing to take yet. Never
        raises: it runs at the top of the background thread."""
        piece = None
        try:
            piece = self._next_piece()
            if piece is None:
                return False
            start, audio = piece
            self._recognize(start, audio)
        except local_engine.Cancelled:
            return False
        except providers.ProviderError as error:
            log.warning("live transcript of %s: %s", self.folder.name, error)
            self._failed(error)
        except Exception as error:  # noqa: BLE001 - top of the background thread: the recording must go on
            log.exception("live transcript of %s", self.folder.name)
            self._failed(error)
        else:
            self._failures, self.error = 0, ""
        if piece is not None:  # a failed piece is skipped: the next round does not run into the same audio again
            self.done_until = start + len(next(iter(audio.values()))) / SAMPLE_RATE
        return True

    def _failed(self, error: Exception) -> None:
        self._failures += 1
        self.error = failures.text(error)
        if self._failures >= MAX_FAILURES:
            self.error = tr("Transcript during the meeting stopped: {reason} The recording goes on.", reason=self.error)
            self.stop()

    def _recognize(self, start: float, audio: dict[str, np.ndarray]) -> None:
        joined, places = engine_audio(audio)
        if not places:  # silence: nothing to load the model for
            return
        with tempfile.TemporaryDirectory(prefix="knotes-live-") as folder:
            path = pathlib.Path(folder) / "piece.wav"
            _write_wav(path, joined)
            parts = recognition.recognize_track(path, len(joined) / SAMPLE_RATE, self.params)
        new_items = piece_phrases(split_words(recognition.words_from(parts), places), audio, start)
        words = list(self.params.vocabulary)
        new_items = [dataclasses.replace(p, text=vocab.fix(p.text, words)) for p in new_items]
        with self._lock:
            self._phrases += new_items
            self.others_heard = self.others_heard or any(p.track == "собеседники" for p in new_items)
            self.version += 1

    def ask(self, question: str) -> str:
        """The minutes model's answer about the part of the meeting already recognized, with recording minutes; the
        cost goes to the spending log. Blocks: called in a background job."""
        params_ = minutes.params(self.config)
        _check(params_)
        phrases_ = self.phrases()
        if not phrases_:
            raise providers.ProviderError(tr("Nothing has been recognized yet. Ask again in half a minute."))
        body = question_body(phrases_, self.others_heard, question, params_)
        if hider := minutes.hider_for(self.folder, params_):
            body["messages"][-1]["content"] = hider.with_note(body["messages"][-1]["content"])
        text, cost = providers.model_answer(params_.service, params_.key, body)
        text = hider.restore(text) if hider else text
        try:
            spending.write(self.folder, catalog.MINUTES_KIND, params_.model, cost)
        except OSError as error:  # the answer is paid and received: it is shown even if the log is not written
            log.warning("cost of a question about %s not logged: %s", self.folder.name, error)
        return text.strip()


def start(config: settings.Settings, folder: pathlib.Path, clock: Callable[[], float]) -> Live | None:
    """The transcript of a recording that has just started, when it is on in the settings and speech is recognized on
    this computer; None otherwise. Never raises: the recording has started already and must go on."""
    if not config.live_transcript:
        return None
    try:
        params_ = recognition.params(config)
        if not params_.provider.local_engine:
            return None
        result = Live(folder, params_, clock, config)
        result.start()
    except Exception:  # noqa: BLE001 - the recording goes on without the transcript during it
        log.exception("live transcript of %s did not start", folder.name)
        return None
    return result
