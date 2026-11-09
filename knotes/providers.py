# SPDX-License-Identifier: GPL-3.0-only
"""Providers: connections to AI services for two functions, speech recognition and minutes.

Each function has its own presets in providers.json: a preset fills in the address (up to /v1, as at OpenAI) and the
model; the address can be changed to any service with an OpenAI-like API, the app adds the request endpoint itself.
Speech recognition: kind "openrouter" sends JSON with base64 audio (OpenRouter), kind "форма" sends multipart as at
OpenAI. Minutes use the /chat/completions request. The model is picked from the service's list or typed in by hand.
Requests go through Windows curl: Cloudflare in front of OpenRouter answers 403 to TLS from Python.
"""

import base64
import dataclasses
import functools
import ipaddress
import json
import logging
import pathlib
import re
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable

from knotes import disk_names
from knotes.language import tr, tr_key

CURL = r"C:\Windows\System32\curl.exe"
LOCAL = "локально"  # kind of the speech recognition preset that runs on this computer
CONFIG_EXTRA = re.compile(r'[\s"\\]')  # a quoted curl config line holds only the key or address itself
DESCRIPTIONS = pathlib.Path(__file__).with_name("providers.json")
SIZE_CODES = (408, 413, 504, 524)
CONNECT_TIMEOUT_S = 20
RECORDING_S = 900  # a request with a recording: the provider may take minutes
SERVICE_S = 30  # key check, balance, model list: a hung server is not waited for longer
TICK_S = 0.5  # how often a running request looks at the cancel and reports progress
# curl exit codes: what happened to the connection
CONNECTION = {
    6: tr_key("Нет интернета или не найден адрес {host}. Проверьте подключение к сети."),
    7: tr_key("Не удалось подключиться к {host}: сервер недоступен или нет интернета."),
    35: tr_key("Не удалось установить защищенное соединение с {host}."),
    52: tr_key("Сервер {host} оборвал соединение, ничего не ответив."),
    55: tr_key("Связь с {host} оборвалась при отправке записи."),
    56: tr_key("Связь с {host} оборвалась при получении ответа."),
    60: tr_key("Сертификат {host} не прошел проверку: сеть может подменять соединение (прокси, антивирус)."),
}
# Other provider refusals: {title} and {reason}, what it answered
REFUSED = tr_key("{title} отказал: {reason}")
NO_DATA = tr_key("{title} не отдал данные: {reason}")
NO_ANSWER = tr_key("{title} не ответил: {reason}")
NO_MODELS = tr_key("{title} не отдал список моделей: {reason}")
# Address {address} without https
PLAIN_RECORDING = tr_key(
    "Адрес {address} без https: ключ и запись ушли бы по сети открыто. Укажите https:// или "
    "сервер на этом компьютере или в своей сети"
)
PLAIN_TRANSCRIPT = tr_key(
    "Адрес {address} без https: ключ и расшифровка ушли бы по сети открыто. Укажите "
    "https:// или сервер на этом компьютере или в своей сети"
)
PLAIN_KEY = tr_key(
    "Адрес {address} без https: ключ ушел бы по сети открыто. Укажите https:// или сервер на этом "
    "компьютере или в своей сети"
)
log = logging.getLogger(__name__)


class ProviderError(Exception):
    """The provider refused or is unavailable; the text can be shown to the user."""


class TemporaryError(ProviderError):
    """A failure that goes away by itself: no connection, the provider is overloaded or down. The request can be
    retried."""


class NetworkError(TemporaryError):
    """No internet, address not found, connection dropped."""


class SizeError(ProviderError):
    """The whole recording did not pass by size or time and has to be cut into pieces."""


class Cancelled(ProviderError):
    """The request was stopped: the program is closing."""


FUNCTIONS = ("распознавание", "протоколы")
ENDPOINTS = ("/audio/transcriptions", "/chat/completions")  # a full address from old settings is understood too


@dataclasses.dataclass(frozen=True)
class Provider:
    """Connection preset; the address goes up to /v1, request endpoints (check, models, balance) are added to it.
    Kind LOCAL is speech recognition on this computer: no address or key, models come from the models folder."""

    id: str
    title: str
    kind: str
    address: str
    model: str
    key: str = ""  # old key name in the key store: keys are stored under the address, an old one is still picked up
    variable: str = ""
    key_needed: bool = True  # an own server usually needs no key
    key_site: str = ""  # where the service issues keys - shown in the first-run tips
    example: str = ""  # hint in the empty address field
    checked: bool = False
    limit_mb: int = 25
    fields: dict = dataclasses.field(default_factory=dict)
    model_fields: dict = dataclasses.field(default_factory=dict)  # model -> its own request fields
    checking: str = ""  # address endpoint used to check the key
    terms: bool = False  # whether it accepts words for accuracy (keyterms)
    models: tuple[tuple[str, str], ...] = ()  # (id, title) when the list is not taken from the service
    models_endpoint: str = ""  # address endpoint with the list of models
    balance: str = ""  # address endpoint with key details: spent and limit
    account: str = ""  # address endpoint with the account balance, for a key without a limit

    @property
    def local_engine(self) -> bool:
        return self.kind == LOCAL


@dataclasses.dataclass(frozen=True)
class Balance:
    remaining: float
    total: float
    source_of: str  # "ключ" (key limit) or "счет" (the key has no limit)
    for_month: float | None = None  # spent with the key this month, per the service's account
    by_key: float | None = None  # spent with the key over all time


@dataclasses.dataclass(frozen=True)
class Request:
    mp3: bytes
    language: str
    terms: tuple[str, ...] = ()
    retention: bool = False
    model: str = ""  # empty means the provider's default model
    diarization: bool = True  # ask for speaker diarization
    cancel: threading.Event | None = dataclasses.field(default=None, compare=False)  # stops the request


def _models(description: list) -> tuple[tuple[str, str], ...]:
    """Preset models: "id" or ["id", "title"]."""
    return tuple((m, m) if isinstance(m, str) else (m[0], m[1]) for m in description)


@functools.cache
def _presets(function: str) -> tuple[Provider, ...]:
    """Presets from providers.json, read once: model titles are asked for every meeting of a summary."""
    descriptions = json.loads(DESCRIPTIONS.read_text(encoding="utf-8"))[function]
    return tuple(Provider(**(disk_names.from_disk(d) | {"models": _models(d.get("модели", ()))})) for d in descriptions)


def list_(function: str = "распознавание") -> list[Provider]:
    return list(_presets(function))


def find(id_: str, function: str = "распознавание") -> Provider:
    for provider in list_(function):
        if provider.id == id_:
            return provider
    raise ProviderError(tr("Unknown provider {id_}", id_=id_))


def base(address: str) -> str:
    """Service address up to /v1: without spaces, a trailing slash or request endpoints."""
    address = address.strip().rstrip("/")
    for ended in ENDPOINTS:
        address = address.removesuffix(ended)
    return address


def with_address(provider: Provider, address: str) -> Provider:
    """The preset with its own address from settings; an empty one keeps the preset address."""
    return dataclasses.replace(provider, address=base(address) or provider.address)


def connection(function: str, id_: str, address: str) -> Provider:
    """Connection from settings: the preset (the first one if unknown) with its own address."""
    presets = list_(function)
    return with_address(next((z for z in presets if z.id == id_), presets[0]), address)


def point(provider: Provider, ended: str) -> str:
    """Full request address; empty if the address is not set."""
    address = base(provider.address)
    return address + ended if address else ""


def model_title(id_: str, function: str = "протоколы") -> str:
    """Model title from the preset lists: "anthropic/claude-sonnet-5.5" -> "Claude Sonnet 5.5"; an unknown one stays
    as is."""
    for provider in list_(function):
        if id_ in dict(provider.models):
            return dict(provider.models)[id_]
    return id_


def model(provider: Provider, request: Request) -> str:
    return request.model or provider.model


def connection_args(address: str) -> list[str]:
    """curl arguments for the address: protocols (https; http only for this computer and the local network; redirects
    only to https), connect timeout and the Windows proxy. curl for Windows does not read the proxy from Windows
    settings, urllib gives the address; with the Windows account (NTLM, Kerberos) curl signs in by itself:
    --proxy-user ":"."""
    link = ["--proto-redir", "=https", "--connect-timeout", str(CONNECT_TIMEOUT_S)]
    if not address.lower().startswith("https://"):
        return ["--proto", "=https,http", *link, "--noproxy", "*"]
    proxy = urllib.request.getproxies().get("https", "")
    if proxy and not urllib.request.proxy_bypass(urllib.parse.urlsplit(address).hostname or ""):
        link += ["--proxy", proxy, "--proxy-anyauth", "--proxy-user", ":"]
    return ["--proto", "=https", *link]


def run_program(
    command: list[str], input_: bytes = b"", cwd: pathlib.Path | None = None, tick: Callable[[], None] | None = None
) -> tuple[int, bytes, bytes]:
    """A program (curl) without a console window: exit code, stdout, stderr. While it runs, tick() is called every
    TICK_S: it reports progress or raises to stop the program, which is then killed."""
    with subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE if input_ else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ) as process_:
        try:
            output, messages = _wait(process_, input_, tick or (lambda: None))
        finally:
            if process_.poll() is None:
                process_.kill()
    return process_.returncode, output, messages


def _wait(process_: subprocess.Popen, input_: bytes, tick: Callable[[], None]) -> tuple[bytes, bytes]:
    data: bytes | None = input_ or None  # the input goes once: communicate() keeps sending it after a timeout
    while True:
        try:
            return process_.communicate(input=data, timeout=TICK_S)
        except subprocess.TimeoutExpired:
            data = None
            tick()


def stop_on(cancel: threading.Event | None) -> Callable[[], None]:
    """tick for run_program: a set cancel stops the request."""

    def tick() -> None:
        if cancel is not None and cancel.is_set():
            raise Cancelled(tr("The request was cancelled: the program is closing."))

    return tick


def _curl(
    key: str, arguments: list[str], address: str, secret_address: bool = False, cancel: threading.Event | None = None
) -> tuple[int, str]:
    """The key (and an address with a secret inside, like a calendar link) is passed as config via stdin so it does
    not show in the command line. A quote, backslash or space in them would add a command of their own to the config.
    Every request is https only, http only to this computer or the local network; the user's _curlrc is not read.
    A --max-time in arguments overrides the one for a recording; a set cancel kills curl."""
    if CONFIG_EXTRA.search(key) or (secret_address and CONFIG_EXTRA.search(address)):
        raise ProviderError(
            tr("The key or link contains a quote, space or line break: check that you copied it whole and nothing else")
        )
    host = urllib.parse.urlsplit(address).hostname or ""
    if insecure_address(address) or not host:
        raise ProviderError(tr(PLAIN_KEY, address=host if secret_address else address))
    tick = stop_on(cancel)
    tick()
    config_text = f'header = "Authorization: Bearer {key}"\n' if key else ""  # an own server does not always need a key
    config_text += f'url = "{address}"\n' if secret_address else ""
    code, output, messages = run_program(
        [
            CURL,
            "-q",
            "-sS",
            "-K",
            "-",
            *connection_args(address),
            "--max-time",
            str(RECORDING_S),
            *arguments,
            "-w",
            "\n%{http_code}",
            *([] if secret_address else [address]),
        ],
        config_text.encode(),
        tick=tick,
    )
    if code:  # an address with a secret puts only the server in the error text: it goes to the log
        raise link(code, messages.decode("utf-8", "replace"), f"https://{host}" if secret_address else address)
    text, http_code = output.decode("utf-8", "replace").rsplit("\n", 1)
    return int(http_code), text


def link(code: int, error: str, address: str) -> ProviderError:
    """A curl failure as plain text. A slow response (not the connection) is a size error: the recording is cut into
    pieces."""
    host = urllib.parse.urlsplit(address).hostname or address
    if code == 28:
        if "Connection timed out" in error or "Resolving timed out" in error:
            return NetworkError(tr("{host} isn't responding: no internet or the server is unavailable.", host=host))
        return SizeError(tr("The provider didn't respond in time"))
    if code in CONNECTION:
        return NetworkError(tr(CONNECTION[code], host=host))
    return NetworkError(tr("Can't reach {host} (curl error {code}).", host=host, code=code))


def _failure(title: str, code: int, text: str, what: str = REFUSED) -> ProviderError:
    """The provider's HTTP refusal as plain text: what happened and what to do. what is the text for other refusals."""
    if code in (401, 403):
        return ProviderError(
            tr("{title} rejected the API key (code {code}). Check the key in Settings.", title=title, code=code)
        )
    if code == 402:
        return ProviderError(
            tr(
                "{title}: no credit left or the key limit is reached. Top up or raise the limit, then try again.",
                title=title,
            )
        )
    if code == 429:
        return TemporaryError(tr("{title} asks to wait: too many requests. Try again in a minute.", title=title))
    if code >= 500:
        return TemporaryError(
            tr("{title} has a temporary failure (code {code}). Try again later.", title=title, code=code)
        )
    return ProviderError(tr(what, title=title, reason=_reason(text)))


def _json(text: str, title: str) -> dict:
    try:
        return json.loads(text)
    except ValueError as error:  # a proxy or a network sign-in page instead of the provider's response
        raise TemporaryError(
            tr(
                "{title} returned an unreadable response: the network may require sign-in. Try again later.",
                title=title,
            )
        ) from error


def _openrouter_body(provider: Provider, request: Request) -> dict:
    body = {
        "model": model(provider, request),
        "input_audio": {"data": base64.b64encode(request.mp3).decode(), "format": "mp3"},
        "diarize": request.diarization,
        "response_format": "verbose_json",
        "timestamp_granularities": ["word"],
        "provider": {"data_collection": "allow" if request.retention else "deny"},
        **provider.fields,
    }
    if request.language:
        body["language"] = request.language
    if request.terms and provider.terms:
        body["keyterms"] = list(request.terms[:100])
    return body


def _send_json(key: str, body: dict, address: str, cancel: threading.Event | None = None) -> tuple[int, str]:
    """The body goes as a file: it would not fit in the command line."""
    with tempfile.TemporaryDirectory() as folder:
        file = pathlib.Path(folder) / "body.json"
        file.write_text(json.dumps(body), encoding="utf-8")
        return _curl(key, ["-H", "Content-Type: application/json", "--data-binary", f"@{file}"], address, cancel=cancel)


def _send(provider: Provider, key: str, request: Request) -> tuple[int, str]:
    address = point(provider, "/audio/transcriptions")
    if provider.kind == "openrouter":
        return _send_json(key, _openrouter_body(provider, request), address, request.cancel)
    with tempfile.TemporaryDirectory() as folder:
        file = pathlib.Path(folder) / "audio.mp3"
        file.write_bytes(request.mp3)
        name = model(provider, request)
        fields = {
            "model": name,
            **({"language": request.language} if request.language else {}),
            **provider.fields,
            **provider.model_fields.get(name, {}),
        }
        arguments = [f"-Ffile=@{file};type=audio/mpeg"]  # other fields as text: "@" and "<" in them do not read a file
        for field, value in fields.items():
            arguments += ["--form-string", f"{field}={value}"]
        return _curl(key, arguments, address, cancel=request.cancel)


def insecure_address(address: str) -> bool:
    """http to another computer, or an address without https:// and a server (curl would pick http itself): the key
    and audio would go over the network in the clear. This computer and the local network (10.*, 172.16-31.*,
    192.168.*, a name without a dot or .local/.lan) are allowed: an own speech recognition server usually runs there.
    An empty address is not set yet: no warning."""
    if not address.strip():
        return False
    parsed = urllib.parse.urlsplit(address.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() not in ("http", "https") or not host:
        return True
    if parsed.scheme.lower() == "https":
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." in host and not host.endswith((".local", ".lan", ".home", ".localhost"))
    return not (ip.is_loopback or ip.is_private or ip.is_link_local)


def _check_address(address: str, failure: str) -> None:
    """failure is the text for an address without https: PLAIN_RECORDING or PLAIN_TRANSCRIPT."""
    if insecure_address(address):
        raise ProviderError(tr(failure, address=address))


def fetch_secret(address: str, title: str) -> str:
    """Text from a link with a secret inside (ICS calendar): https only, the address is not visible in the process
    list."""
    address = re.sub(r"^webcals?://", "https://", address.strip(), flags=re.IGNORECASE)
    if not address.lower().startswith("https://"):
        raise ProviderError(tr("The {title} link must start with https://", title=title))
    code, text = _curl(
        "",
        ["-L", "--proto", "=https", "--max-redirs", "5", "--max-filesize", "20M", "--max-time", "30"],
        address,
        secret_address=True,
    )
    if code != 200:
        raise _failure(title, code, text, NO_DATA)
    return text


def fetch(address: str, seconds: int = 60) -> tuple[int, str]:
    """GET without a key: HTTP code and response text up to 20 MB, following redirects; no connection raises
    NetworkError."""
    return _curl("", ["-L", "--max-redirs", "5", "--max-filesize", "20M", "--max-time", str(seconds)], address)


def recognize(provider: Provider, key: str, request: Request) -> dict:
    _check_address(provider.address, PLAIN_RECORDING)
    if not key and provider.key_needed:
        raise ProviderError(tr("No API key for {title}, set it in Settings", title=provider.title))
    if not provider.address or not model(provider, request):
        raise ProviderError(tr("No server URL or model set for {title}, check Settings", title=provider.title))
    size = len(request.mp3) * 4 // 3 if provider.kind == "openrouter" else len(request.mp3)  # base64 +33%
    if size > provider.limit_mb * 1024 * 1024:
        raise SizeError(tr("The recording exceeds the provider's limit"))
    code, text = _send(provider, key, request)
    if code in SIZE_CODES:
        log.warning("%s did not take the recording by size or time (code %d): %s", provider.title, code, text[:2000])
        raise SizeError(
            tr("{title} did not manage to take the recording whole (code {code})", title=provider.title, code=code)
        )
    if code == 404 and provider.kind == "openrouter" and not request.retention:
        raise _no_retention()
    if code == 400 and request.diarization and "diariz" in _reason(text).lower():
        # the model cannot tell speakers apart: recognize without diarization, everyone becomes one participant
        return recognize(provider, key, dataclasses.replace(request, diarization=False))
    if code != 200:
        raise _failure(provider.title, code, text)
    answer = _json(text, provider.title)
    if not readable(answer):
        log.warning("%s answered in an unknown format: %s", provider.title, text[:2000])
        raise ProviderError(
            tr(
                "{title} returned the transcript in an unexpected format: no words with their times. Check the server "
                "address and the model in Settings.",
                title=provider.title,
            )
        )
    return answer


def _no_retention() -> ProviderError:
    return ProviderError(
        tr("OpenRouter found no provider that doesn't store data. If storing is acceptable, allow it in Settings.")
    )


def model_answer(provider: Provider, key: str, body: dict) -> tuple[str, float]:
    """Request to the language model (chat/completions): the response text and the cost, if the service reports it."""
    _check_address(provider.address, PLAIN_TRANSCRIPT)
    title = provider.title
    code, text = _send_json(key, body, point(provider, "/chat/completions"))
    if code == 404 and (body.get("provider") or {}).get("data_collection") == "deny":
        raise _no_retention()
    if code != 200:
        raise _failure(title, code, text)
    answer = _json(text, title)
    choice = (answer.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    if not content.strip():
        raise ProviderError(tr("{title} returned an empty response. Try again or choose another model.", title=title))
    if choice.get("finish_reason") == "length":
        raise ProviderError(
            tr(
                "{title} cut the response off midway: the model ran out of response length. Choose another model.",
                title=title,
            )
        )
    return content, cost(answer)


def _reason(text: str) -> str:
    """Error text from the provider's response ({"error": {"message": ...}}), without service fields."""
    try:
        error = json.loads(text).get("error")
    except (ValueError, AttributeError):
        if text.lstrip().startswith("<"):
            log.warning("a web page instead of the service response: %s", text[:2000])
            return tr("the server answered with a web page")
        return text[:300]
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error or text)[:300]


def check_key(provider: Provider, key: str) -> str:
    """An empty string if the provider accepted the key; otherwise what is wrong, to show to the user."""
    if not provider.checking:
        return tr("Key check isn't available for this provider")
    if not key and provider.key_needed:
        return tr("Key not set")
    if not provider.address:
        return tr("URL not set")
    try:
        code, text = _curl(key, ["--max-time", str(SERVICE_S)], point(provider, provider.checking))
    except ProviderError as error:
        return str(error)
    return "" if code == 200 else str(_failure(provider.title, code, text))


def _data(title: str, key: str, address: str) -> dict:
    code, body = _curl(key, ["--max-time", str(SERVICE_S)], address)
    if code != 200:
        raise _failure(title, code, body, NO_ANSWER)
    return _json(body, title).get("data") or {}


def balance(provider: Provider, key: str) -> Balance | None:
    """How much money the key has left, or the account for a key without a limit; None if the provider does not
    report it."""
    if not provider.balance or not key:
        return None
    data = _data(provider.title, key, point(provider, provider.balance))
    spent = tuple(None if data.get(field) is None else float(data[field]) for field in ("usage_monthly", "usage"))
    if data.get("limit") is not None:
        return Balance(float(data.get("limit_remaining") or 0), float(data["limit"]), "ключ", *spent)
    if not provider.account:
        return None
    account = _data(provider.title, key, point(provider, provider.account))
    total = float(account.get("total_credits") or 0)
    return Balance(total - float(account.get("total_usage") or 0), total, "счет", *spent)


def models(provider: Provider, key: str) -> list[tuple[str, str]]:
    """The provider's models: (id, title). The list is taken from the provider if it provides one."""
    if not provider.models_endpoint or not provider.address:
        return list(provider.models)
    code, text = _curl(key, ["--max-time", str(SERVICE_S)], point(provider, provider.models_endpoint))
    if code != 200:
        raise _failure(provider.title, code, text, NO_MODELS)
    return [(m["id"], m.get("name") or m["id"]) for m in _json(text, provider.title).get("data", [])]


def words(answer: dict) -> list[dict]:
    """Response words in one shape: start, end, speaker, word.

    If the provider gives only segments with a speaker (Nexara, OpenAI), a segment counts as one "word".
    """
    if answer.get("words"):
        return [
            {
                "начало": float(s["start"]),
                "конец": float(s["end"]),
                "слово": s["word"].strip(),
                "говорящий": s.get("speaker_label") or str(s.get("speaker", "?")),
            }
            for s in answer["words"]
            if s.get("type", "word") == "word" and s["word"].strip()
        ]
    segments = answer.get("segments") or (
        [{"start": 0, "end": answer.get("duration", 0), "text": answer["text"]}] if answer.get("text") else []
    )
    return [
        {
            "начало": float(o["start"]),
            "конец": float(o["end"]),
            "слово": o["text"].strip(),
            "говорящий": str(o.get("speaker", "?")),
        }
        for o in segments
        if o.get("text", "").strip()
    ]


def readable(answer: object) -> bool:
    """Whether words() reads the speech recognition response."""
    try:
        words(answer)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    return True


def cost(answer: dict) -> float:
    return float((answer.get("usage") or {}).get("cost", 0) or 0)
