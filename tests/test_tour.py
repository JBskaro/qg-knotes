# SPDX-License-Identifier: GPL-3.0-only
"""First-run tips: where a tip goes and where its arrow points, steps and how a tour ends, the tips of the meetings
window and of the settings, the default recognition of a new install and which tips an existing user has seen."""

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from knotes import models, providers
from knotes import settings
from knotes import recognition
from knotes.ui import tour
from knotes.ui import settings_calendar
from knotes.ui import connection
from knotes.ui.meetings_window import MeetingsWindow
from knotes.ui.settings_window import SettingsWindow

program = QApplication.instance() or QApplication([])
AREA = QRect(8, 8, 1284, 704)


def _size_of(_side: str) -> tuple[int, int]:
    return 320, 120


def _immediately(_pool: object, work, on_result) -> None:
    on_result(work())


class TestPlacement(unittest.TestCase):
    def test_below_button_arrow_at_its_middle(self):
        button = QRect(14, 14, 210, 36)
        side, place, along = tour.place(button, _size_of, AREA)
        self.assertEqual(side, "top")
        self.assertEqual(place.top(), button.bottom() + tour.GAP)
        self.assertEqual(place.left(), AREA.left())  # at the window edge the tip does not go past it
        self.assertEqual(along, button.center().x() - place.left())

    def test_no_room_below_right_of_item(self):
        item = QRect(20, 650, 200, 40)
        side, place, along = tour.place(item, _size_of, AREA)
        self.assertEqual(side, "left")
        self.assertEqual(place.left(), item.right() + tour.GAP)
        self.assertLessEqual(place.bottom(), AREA.bottom())
        self.assertEqual(along, item.center().y() - place.top())

    def test_centered_without_target(self):
        side, place, _ = tour.place(None, _size_of, AREA)
        self.assertEqual(side, "")
        self.assertLessEqual(abs(place.center().x() - AREA.center().x()), 1)
        self.assertLessEqual(abs(place.center().y() - AREA.center().y()), 1)

    def test_inside_window_at_right_edge(self):
        _, place, _ = tour.place(QRect(1250, 20, 40, 30), _size_of, AREA)
        self.assertLessEqual(place.right(), AREA.right())


class TestSteps(unittest.TestCase):
    def setUp(self):
        self.window = QWidget()
        self.window.resize(800, 600)
        self.button = QPushButton("Начать", self.window)
        self.button.setGeometry(10, 10, 120, 30)
        self.window.show()  # Qt does not send resize events to a hidden window
        self.addCleanup(self.window.close)
        self.result: list[str] = []

    def _tour(self, *steps: tour.Step) -> tour.Tour:
        guide = tour.Tour(self.window, list(steps))
        guide.finished.connect(self.result.append)
        guide.start()
        return guide

    def test_forward_back_and_last_step_button(self):
        guide = self._tour(
            tour.Step("Первый", "Раз", lambda: tour.area_of(self.button, self.window)),
            tour.Step("Второй", "Два"),
            tour.Step("Третий", "Три", action="Открыть"),
        )
        self.assertEqual((guide._title.text(), guide._counter.text()), ("Первый", "1 из 3"))
        self.assertTrue(guide._back.isHidden())
        self.assertGreater(guide.y(), self.button.geometry().bottom())  # below the button it points to
        guide._next.click()
        self.assertEqual(guide._title.text(), "Второй")
        self.assertFalse(guide._back.isHidden())
        guide._back.click()
        self.assertEqual(guide._title.text(), "Первый")
        guide._next.click()
        guide._next.click()
        self.assertEqual(guide._next.text(), "Открыть")
        guide._next.click()
        self.assertEqual(self.result, ["action"])

    def test_tip_text_is_plain(self):
        """A tip may name a model from Hugging Face: its markup is shown as text."""
        guide = self._tour(tour.Step("<b>Модель</b>", "<img src='http://x/y.png'> скачана"))
        self.assertEqual(
            (guide._title.textFormat(), guide._text.textFormat()), (Qt.TextFormat.PlainText, Qt.TextFormat.PlainText)
        )

    def test_done_and_cross(self):
        guide = self._tour(tour.Step("Один", "Раз"))
        self.assertEqual(guide._next.text(), "Готово")
        guide._next.click()
        other = self._tour(tour.Step("Один", "Раз"), tour.Step("Два", "Два"))
        other._close.click()
        self.assertEqual(self.result, ["done", "closed"])

    def test_hidden_target_and_new_window_size(self):
        self.button.hide()
        guide = self._tour(tour.Step("Один", "Раз", lambda: tour.area_of(self.button, self.window)))
        self.assertLessEqual(abs(guide.geometry().center().x() - self.window.rect().center().x()), 1)
        self.window.resize(1000, 700)
        program.processEvents()
        self.assertLessEqual(abs(guide.geometry().center().x() - self.window.rect().center().x()), 1)


class TestDefaultSettings(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.file = pathlib.Path(self._tmp.name) / "настройки.json"

    def test_new_install_recognizes_locally_and_shows_tips(self):
        config, problem = settings.read(self.file)
        self.assertEqual((config.provider, config.tips_shown, problem), (providers.LOCAL, [], ""))

    def test_existing_user_gets_no_tips_and_keeps_choice(self):
        self.file.write_text(json.dumps({"провайдер": "openrouter"}), encoding="utf-8")
        config = settings.load(self.file)
        self.assertEqual(config.provider, "openrouter")
        self.assertEqual(config.tips_shown, list(settings.ALL_TIPS))

    def test_seen_tips_are_remembered(self):
        settings.save(settings.Settings(tips_shown=["окно"]), self.file)
        self.assertEqual(settings.load(self.file).tips_shown, ["окно"])


class TestReadiness(unittest.TestCase):
    def test_local_needs_downloaded_model(self):
        with tempfile.TemporaryDirectory() as folder:
            params = recognition.Params(providers.find(providers.LOCAL), "", models_folder=pathlib.Path(folder))
            self.assertFalse(recognition.done(params))
            with mock.patch.object(models, "status", return_value=models.DOWNLOADED):
                self.assertTrue(recognition.done(params))

    def test_service_needs_key(self):
        service = providers.find("openrouter")
        self.assertFalse(recognition.done(recognition.Params(service, "")))
        self.assertTrue(recognition.done(recognition.Params(service, "sk-test")))


class TestWindowTips(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        for patcher in (
            mock.patch.object(settings, "models_folder", lambda: pathlib.Path(self._tmp.name)),
            mock.patch.object(settings, "address_key", return_value=""),
            mock.patch.object(providers, "models", return_value=[]),
            mock.patch.object(settings_calendar, "run_in_background", _immediately),
            mock.patch.object(connection, "run_in_background", _immediately),
        ):  # no background: the window would not outlive the answer
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_meetings_window(self):
        window = MeetingsWindow()
        window.resize(1300, 720)
        window.show_meetings([], {}, {}, None)
        window.show()
        self.addCleanup(window.close)
        program.processEvents()
        steps = window.tips("Ctrl+Alt+R", recognition_ready=False)
        self.assertEqual([w_.title for w_ in steps], ["Запись встречи", "Работает в фоне", "Распознавание речи"])
        self.assertIn("Ctrl+Alt+R", steps[0].text)
        self.assertEqual(steps[2].action, "Открыть настройки")
        self.assertTrue(all(w_.target() for w_ in (steps[0], steps[2])))
        self.assertIsNone(steps[1].target())  # the tray icon is outside the window: the tip is centered, with the icon
        done = window.tips("", recognition_ready=True)
        self.assertEqual(len(done), 2)  # recognition is already set up: no settings step
        self.assertNotIn("Ctrl", done[0].text)

    def _settings(self, **fields: object) -> SettingsWindow:
        window = SettingsWindow(settings.Settings(**fields))
        window.resize(1100, 700)
        window.show()
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.close)
        program.processEvents()
        return window

    def test_settings_local_model_not_downloaded(self):
        window = self._settings()
        steps = window.tips()
        self.assertEqual([w_.title for w_ in steps], ["Чем распознавать", "Модель", "Протоколы"])
        self.assertIn("не скачана", steps[1].text)
        self.assertFalse(window.recognition.local_model.download.isHidden())
        self.assertIn("Без ключа будет только расшифровка", steps[2].text)
        self.assertTrue(all(w_.target() for w_ in steps))

    def test_settings_service(self):
        steps = self._settings(provider="openrouter").tips()
        self.assertEqual(steps[1].title, "Ключ сервиса")
        self.assertIn("openrouter.ai", steps[1].text)
        self.assertIn("Ключ общий с распознаванием", steps[2].text)

    def test_settings_tips_are_remembered(self):
        window = self._settings()
        changes = []
        window.changed.connect(changes.append)
        window.show_tips()
        window.findChild(tour.Tour)._close.click()
        self.assertEqual(changes[-1].tips_shown, ["настройки"])


if __name__ == "__main__":
    unittest.main()
