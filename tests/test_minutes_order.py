# SPDX-License-Identifier: GPL-3.0-only
"""A paid answer is never lost: the minutes file is written first, the spending log after it; previous minutes rotate
only after a successful answer. Plain failure texts for errors without a message, the default title not stored."""

import datetime
import pathlib
import tempfile
import unittest
from unittest import mock

from knotes import ai_tools, catalog, failures, minutes, providers, settings, spending, transcript


class TestMinutesOrder(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.recordings = pathlib.Path(self._tmp.name)
        patcher = mock.patch.object(
            settings, "load", return_value=settings.Settings(recordings_folder=str(self.recordings))
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = self.recordings / "2026-10-20_10-00"
        self.folder.mkdir()
        transcript.save(
            self.folder,
            [{"начало": 5.0, "конец": 9.0, "кто": "Я", "текст": "Цену берем из прайса."}],
            {"длительность": 60, "провайдер": "тест", "стоимость": 0},
            [],
        )
        service = providers.find("openrouter", "протоколы")
        self.params = minutes.Params(service, "ключ", service.model, "Инструкция")

    def test_paid_minutes_survive_two_unwritable_journals(self):
        write = transcript.write_text
        journals = {self.recordings / name for name in (spending.FILE, spending.RESERVE)}

        def fail_journals(path, text):
            if path in journals:
                raise PermissionError("journal locked")
            write(path, text)

        minutes._paid(self.folder, "# First", "model", providers.Charge(0.1), None)
        with mock.patch.object(transcript, "write_text", side_effect=fail_journals):
            minutes._paid(self.folder, "# Second", "model", providers.Charge(0.2), None)
            self.assertTrue((self.recordings / spending.PENDING).exists())
        for _ in range(2):
            charges = spending.list_(self.recordings, catalog.list_(self.recordings))
            self.assertAlmostEqual(sum(row.how_many for row in charges), 0.3)
        self.assertFalse((self.recordings / spending.PENDING).exists())

    def test_failure_to_keep_history_preserves_user_edits_and_paid_draft(self):
        original = self.folder / minutes.FILE
        original.write_text("# Edited", encoding="utf-8")
        with self.assertRaises(OSError):
            minutes._paid(
                self.folder, "# Generated", "model", providers.Charge(0.12), mock.Mock(side_effect=PermissionError())
            )
        self.assertEqual(original.read_text(encoding="utf-8"), "# Edited")
        (recovery,) = self.folder.glob("протокол.черновик-*.md")
        self.assertEqual(recovery.read_text(encoding="utf-8").strip(), "# Generated")
        self.assertEqual([e.how_many for e in spending.list_(self.recordings, [])], [0.12])

    def test_no_key_changes_nothing(self):
        transcript.mark(self.folder, 7.0, f"{transcript.NOTE} старая заметка")
        with self.assertRaises(providers.ProviderError):
            minutes.compose(self.folder, minutes.Params(self.params.service, "", "м", "И"))
        self.assertEqual(len(transcript.labels(self.folder)), 1)  # the old note is still where it was

    def test_minutes_written_before_spending_and_price_after(self):
        order = []
        with (
            mock.patch.object(providers, "model_answer", return_value=("# Итоги\n", providers.Charge(0.12))),
            mock.patch.object(
                spending,
                "write",
                side_effect=lambda folder, *_, **_kwargs: order.append(
                    ((folder / minutes.FILE).exists(), "стоимость_протокола" in catalog.properties(folder))
                ),
            ),
        ):
            minutes.compose(self.folder, self.params)
        self.assertEqual(order, [(True, False)])  # the file is there, the new price not yet

    def test_failed_request_does_not_rotate_previous_minutes(self):
        (self.folder / minutes.FILE).write_text("# Правка аналитика\n", encoding="utf-8")
        (self.folder / ai_tools.PREVIOUS).write_text("# Прежний\n", encoding="utf-8")
        with (
            mock.patch.object(providers, "model_answer", side_effect=providers.NetworkError("Нет интернета")),
            mock.patch.object(minutes, "params", return_value=self.params),
            self.assertRaises(ai_tools.ToolError),
        ):
            ai_tools.compose_protocol(self.folder.name)
        self.assertEqual(sorted(p.name for p in self.folder.glob("протокол*.md")), [minutes.FILE, ai_tools.PREVIOUS])
        self.assertEqual((self.folder / ai_tools.PREVIOUS).read_text(encoding="utf-8"), "# Прежний\n")

    def test_successful_request_keeps_previous(self):
        (self.folder / minutes.FILE).write_text("# Правка аналитика\n", encoding="utf-8")
        with (
            mock.patch.object(providers, "model_answer", return_value=("# Новый\n", providers.Charge(0.0))),
            mock.patch.object(minutes, "params", return_value=self.params),
        ):
            ai_tools.compose_protocol(self.folder.name)
        self.assertEqual((self.folder / ai_tools.PREVIOUS).read_text(encoding="utf-8"), "# Правка аналитика\n")
        self.assertEqual((self.folder / minutes.FILE).read_text(encoding="utf-8"), "# Новый\n")

    def test_default_title_in_any_language_is_not_stored(self):
        catalog.set_(self.folder, title="Свое")
        year = "" if datetime.date.today().year == 2026 else ".2026"
        for title in (f"Встреча 20.10{year} 10:00", f"Meeting 20.10{year} 10:00 "):
            with self.subTest(title=title), mock.patch.object(catalog.language, "current", return_value="en"):
                ai_tools.set_meeting_info(self.folder.name, title=title)
                self.assertNotIn("название", catalog.properties(self.folder))
                catalog.set_(self.folder, title="Свое")


class TestFailureTexts(unittest.TestCase):
    def test_error_without_message_names_its_kind(self):
        self.assertIn("ValueError", failures.text(ValueError()))
        self.assertNotIn(": .", failures.text(KeyError()))
        self.assertIn("OSError", failures.text(OSError()))


if __name__ == "__main__":
    unittest.main()
