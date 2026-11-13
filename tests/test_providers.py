# SPDX-License-Identifier: GPL-3.0-only
"""Providers: descriptions, request body, response parsing and error codes."""

import dataclasses
import json
import sys
import threading
import time
import unittest
from unittest import mock

from knotes import providers


def curl_process(code: int = 0, stdout: bytes = b"{}\n200", stderr: bytes = b"") -> mock._patch:
    """Stands in for the curl process: Popen gives the exit code and output at once."""
    process_ = mock.MagicMock(returncode=code)
    process_.communicate.return_value = (stdout, stderr)
    process_.__enter__.return_value = process_
    return mock.patch.object(providers.subprocess, "Popen", return_value=process_)


class TestDescriptions(unittest.TestCase):
    def test_descriptions_are_read_and_searched(self):
        ids = [pr.id for pr in providers.list_()]
        self.assertEqual(ids[0], "openrouter")
        self.assertTrue(providers.find("openrouter").checked)

    def test_presets_read_from_disk_once(self):
        """A summary over 500 meetings asks for model titles thousands of times: providers.json is read once."""
        text = providers.DESCRIPTIONS.read_text(encoding="utf-8")
        cache_clear = getattr(providers, "_presets", mock.Mock()).cache_clear
        cache_clear()
        self.addCleanup(cache_clear)
        with mock.patch.object(providers, "DESCRIPTIONS", mock.Mock(read_text=mock.Mock(return_value=text))) as file:
            for _ in range(100):
                providers.model_title("anthropic/claude-sonnet-5.5")
                providers.find("openrouter")
        self.assertEqual(file.read_text.call_count, 2)  # recognition and minutes presets
        presets = providers.list_()
        presets.clear()
        self.assertTrue(providers.list_())  # a caller changing its list does not change the presets

    def test_unknown_provider(self):
        with self.assertRaises(providers.ProviderError):
            providers.find("нет такого")


class TestRequest(unittest.TestCase):
    def setUp(self):
        self.provider = providers.find("openrouter")

    def test_openrouter_body_with_no_retention(self):
        body = providers._openrouter_body(self.provider, providers.Request(b"mp3", "ru", ("НДФЛ",)))
        self.assertEqual(body["provider"], {"data_collection": "deny"})
        self.assertTrue(body["diarize"])
        self.assertNotIn("keyterms", body)  # OpenRouter does not pass them on to Scribe

    def test_terms_only_to_provider_that_accepts_them(self):
        provider = dataclasses.replace(self.provider, terms=True)
        body = providers._openrouter_body(provider, providers.Request(b"mp3", "ru", ("НДФЛ",)))
        self.assertEqual(body["keyterms"], ["НДФЛ"])

    def test_does_not_send_without_key(self):
        with self.assertRaises(providers.ProviderError):
            providers.recognize(self.provider, "", providers.Request(b"mp3", "ru"))

    def test_too_big_file_is_cut(self):
        with self.assertRaises(providers.SizeError):  # 20 MB in base64 exceeds the 25 MB limit
            providers.recognize(self.provider, "к", providers.Request(b"0" * (20 * 1024 * 1024), "ru"))

    def _answer(self, code: int, text: str = "{}") -> object:
        with mock.patch.object(providers, "_send", return_value=(code, text)):
            return providers.recognize(self.provider, "к", providers.Request(b"mp3", "ru"))

    def test_response_codes(self):
        self.assertEqual(self._answer(200, '{"text": "а"}'), {"text": "а"})
        for code, error in [
            (413, providers.SizeError),
            (524, providers.SizeError),
            (401, providers.ProviderError),
            (404, providers.ProviderError),
            (500, providers.ProviderError),
        ]:
            with self.subTest(code=code), self.assertRaises(error):
                self._answer(code)

    def test_size_refusal_without_raw_body(self):
        """Cloudflare answers 524 with its HTML page: the UI and the log get the status without the response body."""
        page = "<!DOCTYPE html><html><head><title>524: A timeout occurred</title></head></html>"
        with self.assertLogs(providers.log, "WARNING") as log_, self.assertRaises(providers.SizeError) as error:
            self._answer(524, page)
        self.assertNotIn("<", str(error.exception))
        self.assertIn("не успел", str(error.exception))
        self.assertIn("code 524", "\n".join(log_.output))
        self.assertNotIn("A timeout", "\n".join(log_.output))

    def test_answer_in_unknown_format_refused_before_cache(self):
        """An own server gave words with "text" instead of "word": a plain refusal at once, not "Unexpected error:
        'word'" later from the cached answer."""
        for text in (
            '{"words": [{"text": "а", "start": 0, "end": 1}]}',
            '{"segments": [{"text": "а", "start": "начало", "end": 1}]}',
            "[1, 2]",
        ):
            with (
                self.subTest(text=text),
                self.assertLogs(providers.log, "WARNING") as log_,
                self.assertRaises(providers.ProviderError) as error,
            ):
                self._answer(200, text)
            self.assertIn("неожиданном виде", str(error.exception))
            self.assertIn("unknown format", "\n".join(log_.output))
            self.assertNotIn(text[:10], "\n".join(log_.output))

    def test_form_goes_via_curl_with_fields(self):
        provider = providers.find("nexara")
        with mock.patch.object(providers, "_curl", return_value=(200, "{}")) as curl:
            providers.recognize(provider, "к", providers.Request(b"mp3", "ru"))
        arguments = curl.call_args.args[1]
        self.assertIn("task=diarize", arguments)
        self.assertTrue(any(a.startswith("-Ffile=@") for a in arguments))


class TestAnswer(unittest.TestCase):
    def test_words_with_speakers_and_without_events(self):
        answer = {
            "words": [
                {"word": "Да ", "start": 1, "end": 2, "speaker_label": "speaker_0"},
                {"word": "[смех]", "start": 2, "end": 3, "type": "audio_event"},
                {"word": "нет", "start": 3, "end": 4, "speaker": 1},
            ]
        }
        self.assertEqual(
            [(s["слово"], s["говорящий"]) for s in providers.words(answer)], [("Да", "speaker_0"), ("нет", "1")]
        )

    def test_segments_as_words(self):
        answer = {
            "segments": [
                {"start": 0, "end": 2, "text": "Здравствуйте!", "speaker": "speaker_0"},
                {"start": 2, "end": 3, "text": " "},
            ]
        }
        waiting = [{"начало": 0, "конец": 2, "слово": "Здравствуйте!", "говорящий": "speaker_0"}]
        self.assertEqual(providers.words(answer), waiting)

    def test_cost(self):
        self.assertEqual(providers.cost({"usage": {"cost": 0.5}}), 0.5)
        self.assertEqual(providers.cost({}), 0.0)


class TestModels(unittest.TestCase):
    def test_model_list_from_provider_and_description(self):
        answer = '{"data": [{"id": "elevenlabs/scribe-v2", "name": "ElevenLabs: Scribe v2"}, {"id": "x/y"}]}'
        with mock.patch.object(providers, "_curl", return_value=(200, answer)):
            self.assertEqual(
                providers.models(providers.find("openrouter"), ""),
                [("elevenlabs/scribe-v2", "ElevenLabs: Scribe v2"), ("x/y", "x/y")],
            )
        self.assertIn(("nexara-ru", "nexara-ru"), providers.models(providers.find("nexara"), ""))
        with (
            mock.patch.object(providers, "_curl", return_value=(500, "{}")),
            self.assertRaises(providers.ProviderError),
        ):
            providers.models(providers.find("openrouter"), "")

    def test_chosen_model_goes_into_request(self):
        body = providers._openrouter_body(
            providers.find("openrouter"), providers.Request(b"mp3", "ru", model="openai/whisper-1")
        )
        self.assertEqual(body["model"], "openai/whisper-1")
        provider = providers.find("openai")
        with mock.patch.object(providers, "_curl", return_value=(200, "{}")) as curl:
            providers.recognize(provider, "к", providers.Request(b"mp3", "ru"))
        self.assertIn("response_format=diarized_json", curl.call_args.args[1])

    def test_model_without_speaker_diarization(self):
        failure = '{"error": {"message": "The selected model does not support diarize"}}'
        answers = [(400, failure), (200, '{"text": "а"}')]
        with mock.patch.object(providers, "_send", side_effect=answers) as send:
            self.assertEqual(
                providers.recognize(providers.find("openrouter"), "к", providers.Request(b"mp3", "ru")), {"text": "а"}
            )
        self.assertFalse(send.call_args.args[2].diarization)

    def test_own_server_without_address(self):
        with self.assertRaises(providers.ProviderError):
            providers.recognize(providers.find("свой"), "к", providers.Request(b"mp3", "ru", model="m"))

    def test_answer_text_only(self):
        self.assertEqual(
            providers.words({"text": "Привет", "duration": 3}),
            [{"начало": 0, "конец": 3, "слово": "Привет", "говорящий": "?"}],
        )


class TestKeyCheck(unittest.TestCase):
    def test_key_check(self):
        provider = providers.find("openrouter")
        with mock.patch.object(providers, "_curl", return_value=(200, "{}")):
            self.assertEqual(providers.check_key(provider, "к"), "")
        with mock.patch.object(providers, "_curl", return_value=(401, "{}")):
            self.assertIn("не принял", providers.check_key(provider, "к"))
        with mock.patch.object(providers, "_curl", side_effect=providers.ProviderError("нет связи")):
            self.assertEqual(providers.check_key(provider, "к"), "нет связи")
        self.assertEqual(providers.check_key(provider, ""), "Ключ не задан")
        self.assertIn("недоступна", providers.check_key(providers.find("nexara"), "к"))

    def test_key_and_account_balance(self):
        pr = providers.find("openrouter")
        key = json.dumps({"data": {"limit": 10, "limit_remaining": 9.81, "usage": 0.19, "usage_monthly": 0.12}})
        with mock.patch.object(providers, "_curl", return_value=(200, key)):
            self.assertEqual(providers.balance(pr, "к"), providers.Balance(9.81, 10.0, "ключ", 0.12, 0.19))
        unlimited = json.dumps({"data": {"limit": None, "usage": 3.5}})
        account = json.dumps({"data": {"total_credits": 50, "total_usage": 12.5}})
        with mock.patch.object(providers, "_curl", side_effect=[(200, unlimited), (200, account)]):
            self.assertEqual(providers.balance(pr, "к"), providers.Balance(37.5, 50.0, "счет", None, 3.5))
        with (
            mock.patch.object(providers, "_curl", return_value=(401, '{"error": {"message": "Нет такого ключа"}}')),
            self.assertRaisesRegex(providers.ProviderError, "не принял ключ"),
        ):
            providers.balance(pr, "к")
        self.assertIsNone(providers.balance(pr, ""))
        self.assertIsNone(providers.balance(providers.find("nexara"), "к"))
        no_account = dataclasses.replace(pr, account="")
        with mock.patch.object(providers, "_curl", return_value=(200, unlimited)):
            self.assertIsNone(providers.balance(no_account, "к"))

    def test_key_and_audio_never_go_over_http_to_foreign_server(self):
        for address, bad_one in (
            ("https://сервер.рф/v1/audio/transcriptions", False),
            ("http://localhost:8000/v1", False),
            ("http://127.0.0.1:8000", False),
            ("http://192.168.1.233:8000", False),
            ("http://10.0.0.5", False),
            ("http://whisper:9000", False),
            ("http://gpu.lan", False),
            ("http://example.com/v1", True),
            ("HTTP://8.8.8.8/v1", True),
            ("", False),
            ("openrouter.ai/api/v1", True),
            ("http:/openrouter.ai/v1", True),
            ("ftp://example.com", True),
        ):
            with self.subTest(address=address):
                self.assertEqual(providers.insecure_address(address), bad_one)
        custom_one = dataclasses.replace(providers.find("свой"), address="http://example.com/v1/audio/transcriptions")
        with (
            mock.patch.object(providers, "_curl") as curl,
            self.assertRaisesRegex(providers.ProviderError, "без https"),
        ):
            providers.recognize(custom_one, "ключ", providers.Request(b"x", "ru"))
        curl.assert_not_called()

    def test_any_keyed_request_only_over_https(self):
        """The key check, the model list and the balance too: no request sends the key in the clear."""
        for address in ("http://openrouter.ai/api/v1/models", "openrouter.ai/api/v1/models", "http:/host/v1"):
            with (
                self.subTest(address=address),
                curl_process() as startup,
                self.assertRaisesRegex(providers.ProviderError, "без https"),
            ):
                providers._curl("ключ", [], address)
            startup.assert_not_called()
        with curl_process(stdout=b"ok\n200") as startup:
            providers._curl("ключ", ["-Fmodel=x"], "https://openrouter.ai/api/v1/models")
        arguments = startup.call_args.args[0]
        self.assertEqual(arguments[1], "-q")  # the user's own _curlrc is not picked up
        self.assertEqual(arguments[arguments.index("--proto") + 1], "=https")
        self.assertEqual(arguments[arguments.index("--proto-redir") + 1], "=https")
        self.assertEqual(
            providers.connection_args("http://localhost:8000/v1")[:2], ["--proto", "=https,http"]
        )  # own server

    def test_proxy_from_windows_settings(self):
        with (
            mock.patch.object(providers.urllib.request, "getproxies", return_value={"https": "http://proxy:3128"}),
            mock.patch.object(providers.urllib.request, "proxy_bypass", return_value=False),
        ):
            link = providers.connection_args("https://openrouter.ai/api/v1")
        self.assertEqual(link[link.index("--proxy") + 1], "http://proxy:3128")
        self.assertIn("--proxy-anyauth", link)
        self.assertIn("--noproxy", providers.connection_args("http://192.168.1.5/v1"))  # local network: no proxy

    def test_form_fields_as_text_not_file(self):
        """A model name "<C:\\file" does not send a file: form fields go as text."""
        pr = dataclasses.replace(providers.find("свой"), address="https://сервер.рф/v1")
        with mock.patch.object(providers, "_curl", return_value=(200, '{"text": ""}')) as curl:
            providers._send(pr, "к", providers.Request(b"x", "ru", model="<C:\\секрет.txt"))
        arguments = curl.call_args.args[1]
        self.assertIn("model=<C:\\секрет.txt", arguments)
        self.assertEqual(arguments[arguments.index("model=<C:\\секрет.txt") - 1], "--form-string")

    def test_calendar_link_not_in_error_text(self):
        with (
            curl_process(3, b"", b"bad"),
            self.assertRaises(providers.ProviderError) as failure,
        ):
            providers.fetch_secret("https://calendar.example.com/ical/СЕКРЕТ/basic.ics", "календарь")
        self.assertNotIn("СЕКРЕТ", str(failure.exception))
        with curl_process(stdout=b"x\n200") as startup:
            providers.fetch_secret("https://calendar.example.com/c.ics", "календарь")
        arguments = " ".join(startup.call_args.args[0])
        for flag in ("--proto =https", "--proto-redir =https", "--max-redirs 5", "--max-filesize"):
            self.assertIn(flag, arguments)

    def test_connection_failures_in_plain_text(self):
        address = "https://openrouter.ai/api/v1/key"
        for code, error, kind, words in (
            (6, "Could not resolve host", providers.NetworkError, "Нет интернета"),
            (7, "Failed to connect", providers.NetworkError, "Не удалось подключиться"),
            (28, "Connection timed out after 20000 ms", providers.NetworkError, "не отвечает"),
            (28, "Operation timed out after 900000 ms", providers.SizeError, "вовремя"),
            (60, "SSL certificate problem", providers.NetworkError, "Сертификат"),
            (99, "что-то", providers.NetworkError, "ошибка curl 99"),
        ):
            with self.subTest(code=code, error=error):
                result = providers.link(code, error, address)
                self.assertIsInstance(result, kind)
                self.assertIn(words, str(result))
        self.assertIn("openrouter.ai", str(providers.link(6, "", address)))

    def test_provider_failures_in_plain_text(self):
        for code, kind, words in (
            (401, providers.ProviderError, "не принял ключ"),
            (402, providers.ProviderError, "деньги"),
            (429, providers.TemporaryError, "подождать"),
            (503, providers.TemporaryError, "временный сбой"),
            (400, providers.ProviderError, "плохой запрос"),
        ):
            with self.subTest(code=code):
                result = providers._failure("OpenRouter", code, '{"error": {"message": "плохой запрос"}}')
                self.assertIsInstance(result, kind)
                self.assertIn(words, str(result))
        self.assertNotIsInstance(providers._failure("OpenRouter", 402, ""), providers.TemporaryError)
        with self.assertRaisesRegex(providers.TemporaryError, "непонятный ответ"):
            providers._json("<html>Вход в сеть</html>", "OpenRouter")
        with (
            mock.patch.object(providers, "_curl", return_value=(200, "<html></html>")),
            self.assertRaises(providers.TemporaryError),
        ):
            providers.recognize(providers.find("openrouter"), "ключ", providers.Request(b"x", "ru"))
        with mock.patch.object(providers, "_curl", return_value=(402, "{}")):
            self.assertIn("деньги", providers.check_key(providers.find("openrouter"), "ключ"))

    def test_curl_waits_briefly_for_connection(self):
        with curl_process() as startup:
            providers._curl("ключ", [], "https://openrouter.ai/api/v1/key")
        command = startup.call_args.args[0]
        self.assertEqual(command[command.index("--connect-timeout") + 1], str(providers.CONNECT_TIMEOUT_S))
        self.assertNotIn("ключ", " ".join(command))  # the key goes only through stdin
        self.assertIn(b"Bearer", startup.return_value.communicate.call_args.kwargs["input"])
        with curl_process(6, b"", b"x"), self.assertRaises(providers.NetworkError):
            providers._curl("ключ", [], "https://openrouter.ai/api/v1/key")

    def test_service_requests_wait_30_seconds(self):
        """A hung own server: "Check" and the model list give up in 30 s, not in the 15 minutes of a recording."""
        provider = dataclasses.replace(providers.find("openrouter"), address="https://сервер.рф/v1")
        for request, answer in (
            (lambda: providers.check_key(provider, "к"), b"{}"),
            (lambda: providers.models(provider, "к"), b'{"data": []}'),
            (lambda: providers.balance(provider, "к"), b'{"data": {"limit": 1}}'),
        ):
            with curl_process(stdout=answer + b"\n200") as startup:
                request()
            command = startup.call_args.args[0]
            limits = [command[n + 1] for n, a in enumerate(command) if a == "--max-time"]
            self.assertEqual(limits[-1], str(providers.SERVICE_S))
        with curl_process(stdout=b'{"text": "a"}\n200') as startup:
            providers.recognize(provider, "к", providers.Request(b"mp3", "ru"))
        command = startup.call_args.args[0]
        self.assertEqual([command[n + 1] for n, a in enumerate(command) if a == "--max-time"], ["900"])

    def test_cancel_stops_hung_request(self):
        """Closing the program stops a request that hangs: the process is killed, not waited for 15 minutes."""
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        with self.assertRaises(providers.Cancelled):
            providers.run_program([sys.executable, "-c", "import time; time.sleep(30)"], tick=providers.stop_on(cancel))
        self.assertLess(time.monotonic() - started, 5)
        result = providers.run_program([sys.executable, "-c", "import sys; print(sys.stdin.read())"], b"input")
        self.assertEqual((result[0], result[1].strip()), (0, b"input"))
        with curl_process() as startup, self.assertRaises(providers.Cancelled):
            providers._curl("к", [], "https://openrouter.ai/api/v1/key", cancel=cancel)
        startup.assert_not_called()

    def test_failure_reason_without_service_fields(self):
        self.assertEqual(
            providers._reason('{"error": {"message": "нет модели", "code": 400}, "user_id": "x"}'), "нет модели"
        )
        self.assertEqual(providers._reason('{"error": "плохо"}'), "плохо")
        self.assertEqual(providers._reason("не json"), "не json")
        with self.assertLogs(providers.log, "WARNING"):  # a proxy or Cloudflare page: to the log, not to the user
            self.assertEqual(providers._reason("<html><body>Bad gateway</body></html>"), "сервер ответил веб-страницей")

    def test_language_optional(self):
        body = providers._openrouter_body(providers.find("openrouter"), providers.Request(b"mp3", ""))
        self.assertNotIn("language", body)


if __name__ == "__main__":
    unittest.main()
