# SPDX-License-Identifier: GPL-3.0-only
"""Connections: each function has a preset, address, API key and model; the address goes up to /v1, the endpoint is
appended automatically; the API key is stored by address and shared for one address; old settings and keys are read."""

import unittest
from unittest import mock

from knotes import providers
from knotes import minutes
from knotes import recognition
from knotes import settings


class TestPresets(unittest.TestCase):
    def test_presets_of_both_functions(self):
        self.assertEqual(
            [z.id for z in providers.list_("распознавание")], ["openrouter", "локально", "nexara", "openai", "свой"]
        )
        self.assertEqual([z.id for z in providers.list_("протоколы")], ["openrouter", "openai", "свой", "lmstudio"])
        self.assertIn("Ollama", providers.find("свой", "протоколы").title)
        self.assertEqual(providers.find("openrouter").address, "https://openrouter.ai/api/v1")

    def test_address_up_to_v1_and_endpoints(self):
        for address, base in (
            (" https://мой/v1/audio/transcriptions ", "https://мой/v1"),
            ("http://localhost:11434/v1/chat/completions", "http://localhost:11434/v1"),
            ("http://localhost:1234/v1/", "http://localhost:1234/v1"),
            ("", ""),
        ):
            with self.subTest(address=address):
                self.assertEqual(providers.base(address), base)
        self.assertEqual(
            providers.point(providers.find("openrouter"), "/audio/transcriptions"),
            "https://openrouter.ai/api/v1/audio/transcriptions",
        )
        self.assertEqual(
            providers.point(providers.find("свой"), "/audio/transcriptions"), ""
        )  # the own server address is not set

    def test_requests_to_own_endpoints(self):
        with mock.patch.object(providers, "_curl", return_value=(200, '{"text": "а"}')) as curl:
            providers.recognize(providers.find("nexara"), "к", providers.Request(b"mp3", "ru"))
        self.assertEqual(curl.call_args.args[2], "https://api.nexara.ru/v1/audio/transcriptions")
        with mock.patch.object(providers, "_curl", return_value=(200, "{}")) as curl:
            self.assertEqual(providers.check_key(providers.find("openrouter"), "к"), "")
        self.assertEqual(curl.call_args.args[2], "https://openrouter.ai/api/v1/key")

    def test_own_server_checked_even_without_key(self):
        custom_one = providers.with_address(providers.find("свой", "протоколы"), "http://localhost:11434/v1")
        with mock.patch.object(providers, "_curl", return_value=(200, '{"data": [{"id": "qwen3:14b"}]}')) as curl:
            self.assertEqual(providers.check_key(custom_one, ""), "")
            self.assertEqual(providers.models(custom_one, ""), [("qwen3:14b", "qwen3:14b")])
        self.assertEqual(curl.call_args.args[2], "http://localhost:11434/v1/models")

    def test_openrouter_minutes_models_with_names(self):
        models = dict(providers.models(providers.find("openrouter", "протоколы"), ""))
        self.assertEqual(models["anthropic/claude-sonnet-5.5"], "Claude Sonnet 5.5")


class TestKeyByAddress(unittest.TestCase):
    def test_key_by_address_and_old_key_moves(self):
        store = {("QG Knotes", "OpenRouter"): "старый"}
        put = lambda s, i, z: store.__setitem__((s, i), z)  # noqa: E731
        with (
            mock.patch.object(settings.keyring, "get_password", side_effect=lambda s, i: store.get((s, i))),
            mock.patch.object(settings.keyring, "set_password", side_effect=put),
        ):
            self.assertEqual(settings.address_key("https://openrouter.ai/api/v1", "OpenRouter"), "старый")
            self.assertEqual(store[("QG Knotes", "openrouter.ai")], "старый")  # now stored under the address
            settings.set_address_key("http://localhost:11434/v1", "к2")
            self.assertEqual(settings.address_key("http://localhost:11434/v1/chat/completions"), "к2")  # same address
            self.assertEqual(settings.address_key(""), "")


class TestConnectionSettings(unittest.TestCase):
    def test_recognition_from_settings_and_old_full_address(self):
        with mock.patch.object(settings, "address_key", return_value="к") as key:
            custom_one = recognition.params(
                settings.Settings(provider="свой", address=" https://мой/v1/audio/transcriptions ")
            )
            regular = recognition.params(settings.Settings(provider="openrouter"))
        self.assertEqual(
            (custom_one.provider.address, regular.provider.address), ("https://мой/v1", "https://openrouter.ai/api/v1")
        )
        self.assertEqual(key.call_args_list[-1].args[:2], ("https://openrouter.ai/api/v1", "OpenRouter"))

    def test_minutes_from_settings(self):
        with mock.patch.object(settings, "address_key", return_value="к"):
            by_default = minutes.params(settings.Settings())
            custom_one = minutes.params(
                settings.Settings(
                    minutes_service="свой", minutes_address="http://192.168.1.233:11434/v1/chat/completions"
                )
            )
        self.assertEqual((by_default.service.id, by_default.model), ("openrouter", "anthropic/claude-sonnet-5.5"))
        self.assertEqual((custom_one.service.address, custom_one.model), ("http://192.168.1.233:11434/v1", ""))


if __name__ == "__main__":
    unittest.main()
