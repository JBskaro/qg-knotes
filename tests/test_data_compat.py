# SPDX-License-Identifier: GPL-3.0-only
"""The user's files written by earlier versions stay readable and keep their format: settings, meeting files
(properties, transcript, names, marks, notes, minutes, recording and screen lists), tasks, projects, spending, own
models and the mark of a downloaded model. The samples in fixtures/legacy were written before the code was renamed."""

import datetime
import json
import pathlib
import shutil
import tempfile
import unittest

from knotes import catalog, models, projects, minutes, transcript, screen, tasks, notes, recording, spending
from knotes import settings

SAMPLES = pathlib.Path(__file__).with_name("fixtures") / "legacy"


def _keys(data: object) -> object:
    """The shape of JSON data: keys of every object, without the values."""
    if isinstance(data, dict):
        return {k: _keys(z) for k, z in data.items()}
    if isinstance(data, list):
        return [_keys(z) for z in data[:1]]
    return None


class TestLegacyData(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = pathlib.Path(self._tmp.name) / "legacy"
        shutil.copytree(SAMPLES, self.folder)
        self.recordings = self.folder / "records"
        self.meeting = self.recordings / "2026-10-20_10-00"

    def test_settings(self):
        file = self.folder / "настройки.json"
        previous_items = json.loads(file.read_text(encoding="utf-8"))
        config = settings.load(file)
        self.assertEqual(
            (config.provider, config.model, config.microphone, config.hotkey),
            ("openrouter", "whisper-1", "Гарнитура", "Ctrl+Alt+K"),
        )
        self.assertEqual(
            (config.projects, config.project_looks, config.window_layout),
            (["Склад"], {"Склад": {"цвет": 2, "значок": "папка"}}, {"скрыто": ["встречи"]}),
        )
        self.assertEqual(
            (config.tips_shown, config.check_for_updates, config.minutes_service),
            (["мастер", "окно"], False, "openrouter"),  # the 0.20 "AI assistant" preset: the assistant works always
        )
        self.assertEqual(
            (config.my_name, config.record_screen, config.video_quality, config.recordings),
            ("Дмитрий", True, "эконом", pathlib.Path("C:\\Записи")),
        )
        self.assertEqual(config.vocabulary, [{"правильно": "ШЕ", "варианты": ["ше"]}])
        settings.save(config, file)
        previous_items["протокол_сервис"] = "openrouter"
        previous_items |= {"скрывать_личные_данные": True, "скрывать_имена": False}  # fields added after 0.21
        previous_items["расшифровка_во_время_встречи"] = False  # a later setting, written with its default
        previous_items["local_gpu"] = True
        self.assertEqual(json.loads(file.read_text(encoding="utf-8")), previous_items)  # the same file format

    def test_meeting(self):
        meeting = catalog.meeting(self.meeting)
        self.assertEqual(
            (meeting.heading, meeting.project, meeting.status), ("Загрузка остатков", "Склад", catalog.READY)
        )
        self.assertEqual(catalog.properties(self.meeting)["модель_протокола"], "anthropic/claude-sonnet-5.5")
        phrases = transcript.phrases(self.meeting)
        self.assertEqual([f["текст"] for f in phrases["фразы"]], ["Цену берем из прайса.", "Да, из прайса."])
        self.assertEqual(transcript.names(self.meeting), {"Собеседник 1": "Анна"})
        self.assertEqual([m["время"] for m in transcript.labels(self.meeting)], [30.0])
        notepad = notes.read(self.meeting)
        self.assertEqual([(p.text, p.discussed) for p in notepad.agenda], [("Цены", True), ("Остатки", False)])
        self.assertEqual(minutes.result(self.meeting), "1 решение · 1 задача")
        for name, write in (
            (
                "встреча.json",
                lambda: catalog.set_(self.meeting, title="Загрузка остатков", project="Склад", seconds=60.0),
            ),
            ("имена.json", lambda: transcript.set_names(self.meeting, {"Собеседник 1": "Анна"})),
            ("заметки.md", lambda: notes.write(self.meeting, notepad)),
        ):
            old = (self.meeting / name).read_text(encoding="utf-8")
            write()
            self.assertEqual((self.meeting / name).read_text(encoding="utf-8"), old, name)
        labels = json.loads((self.meeting / "метки.json").read_text(encoding="utf-8"))
        transcript.mark(self.meeting, 40.0)
        self.assertEqual(_keys(json.loads((self.meeting / "метки.json").read_text(encoding="utf-8"))), _keys(labels))

    def test_interrupted_recording(self):
        interrupted = self.recordings / "2026-10-21_09-00"
        self.assertIn(interrupted, recording.interrupted_all(self.recordings))
        self.assertEqual(screen.read_chunks(interrupted), [{"файл": "экран_01.mkv", "начало": 0.5}])

    def test_tasks_projects_spending(self):
        file = self.recordings / tasks.FILE
        previous_items = json.loads(file.read_text(encoding="utf-8"))
        custom = next(z for z in tasks.tasks(self.recordings) if z.text == "Проверить загрузку")
        self.assertEqual(
            (custom.who, custom.due, custom.status, custom.priority, custom.description),
            ("Анна", datetime.date(2026, 10, 30), tasks.IN_PROGRESS, "высокий", "После обновления"),
        )
        self.assertEqual(list(custom.labels), ["срочно"])
        self.assertEqual([(p.text, p.is_done) for p in custom.checklist], [("Цены", True), ("Остатки", False)])
        tasks.edit(self.recordings, custom.key, tasks.Edit(description="После обновления"))
        self.assertEqual(_keys(json.loads(file.read_text(encoding="utf-8"))), _keys(previous_items))
        project_notes = projects.notes(self.recordings, "Склад")
        self.assertEqual((project_notes.text, project_notes.for_model), ("У клиента ЗУП 3.1.", False))
        self.assertEqual(
            (projects.for_meeting(self.recordings, "Склад"), projects.kind(self.recordings, "Склад")),
            ("Спросить про остатки", "Показ доработки"),
        )
        projects_file = self.recordings / projects.FILE
        previous_projects = projects_file.read_text(encoding="utf-8")
        projects.set_(self.recordings, "Склад", project_notes)
        projects.set_for_meeting(self.recordings, "Склад", "Спросить про остатки")
        projects.set_kind(self.recordings, "Склад", "Показ доработки")
        self.assertEqual(projects_file.read_text(encoding="utf-8"), previous_projects)
        expenses_ = spending.list_(self.recordings, [])
        self.assertIn(("anthropic/claude-sonnet-5.5", 0.15), [(t_.model, t_.how_many) for t_ in expenses_])
        lines = (self.recordings / spending.FILE).read_text(encoding="utf-8").splitlines()
        spending.write(self.meeting, catalog.SPEECH, "elevenlabs/scribe-v2", 0.02)
        new_one = (self.recordings / spending.FILE).read_text(encoding="utf-8").splitlines()[-1]
        self.assertEqual(set(json.loads(new_one)), set(json.loads(lines[-1])))

    def test_own_models_and_downloaded(self):
        folder = self.folder / "models"
        custom = next(m for m in models.custom_items(folder) if m.id == "hf-test-model")
        self.assertEqual(
            (custom.title, custom.kind, custom.languages, [f.name for f in custom.files]),
            ("Тестовая модель", "whisper", ("ru",), ["model.onnx"]),
        )
        self.assertEqual(models.status(folder, custom), models.DOWNLOADED)
        previous_items = json.loads((folder / "свои.json").read_text(encoding="utf-8"))
        models.remember(folder, custom)
        self.assertEqual(json.loads((folder / "свои.json").read_text(encoding="utf-8")), previous_items)


if __name__ == "__main__":
    unittest.main()
