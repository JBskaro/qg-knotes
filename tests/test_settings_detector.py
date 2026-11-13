# SPDX-License-Identifier: GPL-3.0-only
"""Settings (file and keys) and the meeting detector by the microphone registry."""

import pathlib
import tempfile
import unittest
from unittest import mock

from knotes import detector
from knotes import settings


class TestSettings(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "настройки.json"
            settings.save(settings.Settings(provider="openrouter", model="whisper-1", delete_audio_after=30), path)
            loaded = settings.load(path)
            self.assertEqual((loaded.model, loaded.delete_audio_after), ("whisper-1", 30))

    def test_meeting_program_names(self):
        self.assertEqual(
            [settings.program_name(p) for p in ("yandextelemost.exe", "Teams.exe", "msedge.exe", "Sber.exe")],
            ["Телемост", "Teams", "браузер", "Sber"],
        )
        self.assertIn("MSTeams_8wekyb3d8bbwe", settings.MEETING_APPS)

    def test_no_file_and_extra_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "настройки.json"
            self.assertEqual(settings.load(path), settings.Settings())
            path.write_text('{"язык": "en", "старое_поле": 1}', encoding="utf-8")
            self.assertEqual(settings.load(path).language, "en")

    def test_ui_language_before_start_leaves_broken_file(self):
        """Reading the UI language leaves a broken file in place, so the "Settings reset" message reaches the owner."""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "настройки.json"
            self.assertEqual(settings.ui_language(path), "")
            path.write_text('{"язык_интерфейса": "en"}', encoding="utf-8")
            self.assertEqual(settings.ui_language(path), "en")
            path.write_text("{испорчен", encoding="utf-8")
            self.assertEqual(settings.ui_language(path), "")
            self.assertTrue(path.exists())
            self.assertTrue(settings.read(path)[1])  # the broken file message is left for the program start

    def test_key_from_store_else_from_variable(self):
        with mock.patch.object(settings.keyring, "get_password", return_value="из хранилища"):
            self.assertEqual(settings.key("OpenRouter", "ПЕРЕМЕННАЯ_ТЕСТА"), "из хранилища")
        with (
            mock.patch.object(settings.keyring, "get_password", return_value=None),
            mock.patch.dict(settings.os.environ, {"ПЕРЕМЕННАЯ_ТЕСТА": "из переменной"}),
        ):
            self.assertEqual(settings.key("OpenRouter", "ПЕРЕМЕННАЯ_ТЕСТА"), "из переменной")

    def test_empty_key_is_deleted(self):
        no_key = settings.keyring.errors.PasswordDeleteError
        with mock.patch.object(settings.keyring, "delete_password", side_effect=no_key) as delete:
            settings.set_key("OpenRouter", "")
        delete.assert_called_once()


class TestDetector(unittest.TestCase):
    STATE = {
        r"C:\Program Files\Zoom\bin\Zoom.exe": True,
        r"C:\Program Files\Google\Chrome\chrome.exe": False,
        "MSTeams_8wekyb3d8bbwe": True,
        r"C:\Встречи\Встречи.exe": True,
    }

    def test_meetings_without_own_program(self):
        result = detector.meetings(
            self.STATE,
            ["zoom.exe", "chrome.exe", "MSTeams_8wekyb3d8bbwe", "Встречи.exe"],
            custom_one=r"C:\Встречи\Встречи.exe",
        )
        self.assertEqual(result, {"Zoom.exe", "MSTeams_8wekyb3d8bbwe"})

    def test_offers_once_after_long_microphone(self):
        d = detector.Detector(["Zoom.exe"])
        self.assertEqual(d.step({"Zoom.exe": True}, 0, False).suggest, set())
        self.assertEqual(d.step({"Zoom.exe": True}, detector.LONGER_S, False).suggest, {"Zoom.exe"})
        self.assertEqual(d.step({"Zoom.exe": True}, 30, False).suggest, set())
        self.assertEqual(d.step({"Zoom.exe": False}, 31, False).finished_items, set())  # maybe they just muted
        step = d.step({"Zoom.exe": False}, 31 + detector.END_S, False)
        self.assertEqual((step.suggest, step.finished_items), (set(), {"Zoom.exe"}))
        self.assertEqual(d.step({"Zoom.exe": False}, 60 + detector.END_S, False).finished_items, set())  # only once

    def test_new_teams_capture_without_registry_activity_offers_recording(self):
        device = object()
        devices = mock.Mock()
        devices.GetCount.return_value = 1
        devices.Item.return_value = device
        enumerator = mock.Mock()
        enumerator.EnumAudioEndpoints.return_value = devices
        apps = [detector.TEAMS_PACKAGE]
        watcher = detector.Detector(apps)
        with (
            mock.patch.object(detector, "_read", side_effect=lambda _: {detector.TEAMS_PACKAGE: False}),
            mock.patch.object(detector.AudioUtilities, "GetDeviceEnumerator", return_value=enumerator),
            mock.patch.object(detector, "_capture_processes", return_value={42}) as capture,
            mock.patch.object(detector.system, "process_path", return_value=r"C:\Teams\ms-teams.exe"),
        ):
            self.assertFalse(detector.read(["Zoom.exe"])[detector.TEAMS_PACKAGE])
            capture.assert_not_called()
            self.assertEqual(watcher.step(detector.read(apps), 0, False).suggest, set())
            self.assertEqual(watcher.step(detector.read(apps), detector.LONGER_S, False).suggest, set(apps))
            capture.assert_called_with(device)

    def test_decline_lasts_until_meeting_end(self):
        """Muted: the program released the microphone, unmuted: took it again; it is the same meeting, do not offer
        a second time. A new meeting comes after a long break."""
        d = detector.Detector(["Zoom.exe"])
        d.step({"Zoom.exe": True}, 0, False)
        self.assertEqual(d.step({"Zoom.exe": True}, 5, False).suggest, {"Zoom.exe"})  # answered "Not now"
        for now, busy in ((10, False), (40, False), (45, True), (60, True), (90, False), (200, True)):
            with self.subTest(now=now):
                self.assertEqual(d.step({"Zoom.exe": busy}, now, False).suggest, set())
        d.step({"Zoom.exe": False}, 300, False)
        new_one = 300 + detector.NEW_S + 1
        d.step({"Zoom.exe": True}, new_one, False)
        self.assertEqual(d.step({"Zoom.exe": True}, new_one + detector.LONGER_S, False).suggest, {"Zoom.exe"})

    def test_offers_within_seconds(self):
        """With the program polling, the offer appears no later than 4 seconds after the meeting takes the
        microphone."""
        d, now = detector.Detector(["Zoom.exe"]), 0.5  # the microphone is taken between polls
        while not d.step({"Zoom.exe": True}, now, False).suggest:
            now += detector.POLL_S
        self.assertLessEqual(now, 4)

    def test_does_not_offer_during_and_right_after_recording(self):
        d = detector.Detector(["Zoom.exe"])
        d.step({"Zoom.exe": True}, 0, True)
        self.assertEqual(d.step({"Zoom.exe": True}, 20, True).suggest, set())
        d.recording_ended(20)
        self.assertEqual(d.step({"Zoom.exe": True}, 30, False).suggest, set())
        d.step({"Zoom.exe": False}, 40, False)
        new_one = 40 + detector.NEW_S + 10
        d.step({"Zoom.exe": True}, new_one, False)
        self.assertEqual(d.step({"Zoom.exe": True}, new_one + 10, False).suggest, {"Zoom.exe"})

    def test_registry_is_read(self):
        status = detector.read()
        self.assertIsInstance(status, dict)
        self.assertTrue(all(isinstance(z, bool) for z in status.values()))


if __name__ == "__main__":
    unittest.main()
