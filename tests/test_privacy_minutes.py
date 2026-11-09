# SPDX-License-Identifier: GPL-3.0-only
"""Personal data and the minutes: a cloud model gets labels, a model on this computer gets the text as is, the minutes
on disk have the real values; the user's own assistant over MCP gets labels too and save_protocol puts the values
back by the table kept in the meeting folder."""

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from knotes import ai_tools, catalog, disk_names, minutes, notes, privacy, providers, settings, transcript

PHONE, MAIL = "+7 (912) 345-67-89", "anna@romashka.ru"


class TestPrivacyMinutes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.recordings = pathlib.Path(self._tmp.name)
        self.config = settings.Settings(recordings_folder=str(self.recordings), my_name="Дмитрий")
        patcher = mock.patch.object(settings, "load", side_effect=lambda: self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = self.recordings / "2026-10-20_10-00"
        self.folder.mkdir()
        transcript.save(
            self.folder,
            [
                {"начало": 5.0, "конец": 9.0, "кто": "Я", "текст": "Анна, продиктуйте телефон."},
                {"начало": 10.0, "конец": 19.0, "кто": "Собеседник 1", "текст": f"Мой {PHONE}, почта {MAIL}."},
            ],
            {"длительность": 60, "провайдер": "тест", "стоимость": 0},
            [],
        )
        transcript.set_names(self.folder, {"Собеседник 1": "Анна Смирнова"})
        self.cloud = providers.find("openrouter", "протоколы")
        self.local = providers.connection("протоколы", "свой", "")

    def _compose(self, params: minutes.Params, answer: str) -> str:
        with mock.patch.object(providers, "model_answer", return_value=(answer, 0.0)) as call:
            minutes.compose(self.folder, params)
        return call.call_args.args[2]["messages"][1]["content"]

    def test_cloud_model_gets_labels_minutes_get_values(self):
        params = minutes.Params(self.cloud, "к", self.cloud.model, "Инструкция")
        sent = self._compose(params, "1. Позвонить [ЧЕЛОВЕК_1] по [ТЕЛЕФОН_1], письмо на [EMAIL_1].")
        self.assertNotIn(PHONE, sent)
        self.assertNotIn(MAIL, sent)
        self.assertIn("[ТЕЛЕФОН_1]", sent)
        self.assertIn("Анна Смирнова", sent)  # names only with the separate switch
        self.assertTrue(sent.startswith(privacy.NOTE))
        saved = (self.folder / minutes.FILE).read_text(encoding="utf-8")
        self.assertIn(f"по {PHONE}, письмо на {MAIL}.", saved)
        self.assertIn("[ЧЕЛОВЕК_1]", saved)  # a label the table does not know stays

    def test_names_hidden_with_their_switch(self):
        params = minutes.Params(self.cloud, "к", self.cloud.model, "Инструкция", "Дмитрий", hide_names=True)
        sent = self._compose(params, "Участники - [ЧЕЛОВЕК_1], [ЧЕЛОВЕК_2]")
        for name in ("Анна", "Смирнова", "Дмитрий"):
            self.assertNotIn(name, sent)
        self.assertIn("Участники - Дмитрий, Анна Смирнова", (self.folder / minutes.FILE).read_text(encoding="utf-8"))

    def test_model_on_this_computer_or_switch_off_gets_text_as_is(self):
        for params in (
            minutes.Params(self.local, "", "qwen3:14b", "Инструкция", hide_names=True),
            minutes.Params(self.cloud, "к", self.cloud.model, "Инструкция", hide_data=False),
        ):
            with self.subTest(service=params.service.id):
                sent = self._compose(params, "# Итоги\n")
                self.assertIn(PHONE, sent)
                self.assertNotIn(privacy.NOTE, sent)

    def test_settings_fields_and_their_disk_names(self):
        self.assertEqual((settings.Settings().hide_personal_data, settings.Settings().hide_names), (True, False))
        stored = disk_names.to_disk({"hide_personal_data": False, "hide_names": True})
        self.assertEqual(stored, {"скрывать_личные_данные": False, "скрывать_имена": True})
        params = minutes.params(settings.Settings(hide_personal_data=False, hide_names=True))
        self.assertEqual((params.hide_data, params.hide_names), (False, True))

    def test_assistant_gets_labels_and_save_restores(self):
        notes.add(self.folder, 12, f"перезвонить {PHONE}")
        job = ai_tools.get_protocol_task(self.folder.name)
        piece = ai_tools.get_transcript(self.folder.name)
        everything = json.dumps([job, piece], ensure_ascii=False)
        self.assertNotIn(PHONE, everything)
        self.assertNotIn(MAIL, everything)
        self.assertIn("[ТЕЛЕФОН_1]", job["input"])
        self.assertIn("Мой [ТЕЛЕФОН_1], почта [EMAIL_1].", piece["текст"])
        self.assertEqual(piece["встреча"], self.folder.name)
        table = json.loads((self.folder / privacy.LABELS).read_text(encoding="utf-8"))
        self.assertEqual(table, {"[ТЕЛЕФОН_1]": PHONE, "[EMAIL_1]": MAIL})
        ai_tools.save_protocol(self.folder.name, "1. Позвонить по [ТЕЛЕФОН_1]. [00:10]")
        self.assertIn(PHONE, (self.folder / minutes.FILE).read_text(encoding="utf-8"))
        ai_tools.update_protocol(self.folder.name, "1. Написать на [EMAIL_1].")
        self.assertIn(MAIL, (self.folder / minutes.FILE).read_text(encoding="utf-8"))

    def test_assistant_names_hidden_in_whole_answer(self):
        self.config = settings.Settings(recordings_folder=str(self.recordings), hide_names=True)
        piece = ai_tools.get_transcript(self.folder.name)
        self.assertNotIn("Анна", json.dumps(piece, ensure_ascii=False))
        self.assertEqual(piece["участники"], ["Я", "[ЧЕЛОВЕК_1]"])
        one = ai_tools.get_transcript(self.folder.name, speaker="[ЧЕЛОВЕК_1]")  # the assistant knows only the label
        self.assertEqual(one["текст"], "[00:10] [ЧЕЛОВЕК_1]: Мой [ТЕЛЕФОН_1], почта [EMAIL_1].")

    def test_assistant_with_switch_off_gets_text_as_is(self):
        self.config = settings.Settings(recordings_folder=str(self.recordings), hide_personal_data=False)
        self.assertIn(PHONE, ai_tools.get_transcript(self.folder.name)["текст"])
        self.assertFalse((self.folder / privacy.LABELS).exists())
        self.assertNotIn("скрытые", json.dumps(catalog.properties(self.folder), ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
