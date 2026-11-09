# SPDX-License-Identifier: GPL-3.0-only
"""API keys by server: a preset's key never goes to another server the preset address was changed to."""

import os
import unittest
from unittest import mock

from knotes import settings

OPENROUTER, VARIABLE = "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"
RESELLER = "https://api.proxyapi.ru/openrouter/v1"


class TestKeyByServer(unittest.TestCase):
    def setUp(self):
        self.store = {}
        patches = (
            mock.patch.object(settings.keyring, "get_password", side_effect=lambda s, i: self.store.get((s, i))),
            mock.patch.object(settings.keyring, "set_password", side_effect=self._put),
            mock.patch.object(settings.keyring, "delete_password", side_effect=self._delete),
            mock.patch.dict(os.environ, {VARIABLE: "из переменной"}),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _put(self, service: str, name: str, value: str) -> None:
        self.store[(service, name)] = value

    def _delete(self, service: str, name: str) -> None:
        if (service, name) not in self.store:
            raise settings.keyring.errors.PasswordDeleteError(name)
        del self.store[(service, name)]

    def test_custom_address_does_not_get_preset_key(self):
        self.store[(settings.SERVICE, "OpenRouter")] = "старый"
        self.assertEqual(settings.address_key(RESELLER, "OpenRouter", VARIABLE), "")
        self.assertEqual(self.store, {(settings.SERVICE, "OpenRouter"): "старый"})

    def test_preset_server_gets_old_key_and_it_moves(self):
        self.store[(settings.SERVICE, "OpenRouter")] = "старый"
        self.assertEqual(settings.address_key(OPENROUTER, "OpenRouter", VARIABLE), "старый")
        self.assertEqual(self.store, {(settings.SERVICE, "openrouter.ai"): "старый"})
        self.assertEqual(settings.address_key(OPENROUTER, "OpenRouter", VARIABLE), "старый")

    def test_variable_only_for_preset_server(self):
        self.assertEqual(settings.address_key(OPENROUTER, "OpenRouter", VARIABLE), "из переменной")
        self.assertEqual(settings.address_key(RESELLER, "OpenRouter", VARIABLE), "")
        self.assertEqual(settings.address_key("https://api.proxyapi.ru/openai/v1", "OpenAI", "OPENAI_API_KEY"), "")

    def test_key_saved_for_custom_server_is_its_own(self):
        settings.set_address_key(RESELLER, "ключ посредника")
        self.assertEqual(settings.address_key(RESELLER, "OpenRouter", VARIABLE), "ключ посредника")

    def test_own_server_preset_keeps_its_old_key_on_any_address(self):
        """The own server preset has no fixed server: its old key was entered for the user's own server."""
        self.store[(settings.SERVICE, "Сервер протоколов")] = "свой"
        self.assertEqual(settings.address_key("http://192.168.1.5:11434/v1", "Сервер протоколов"), "свой")
        self.assertEqual(self.store, {(settings.SERVICE, "192.168.1.5:11434"): "свой"})


if __name__ == "__main__":
    unittest.main()
