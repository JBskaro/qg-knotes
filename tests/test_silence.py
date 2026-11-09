# SPDX-License-Identifier: GPL-3.0-only
"""Silence on a recording track: a warning only after the track's limit, gone once it sounds; the warning says what
to check."""

import unittest

from knotes import silence


class TestSilence(unittest.TestCase):
    def test_warning_after_the_limit_and_gone_with_sound(self):
        watch = silence.Watch()
        quiet = {"микрофон": 10, "собеседники": 0}
        self.assertEqual(watch.step(quiet, 100.0), {})
        self.assertEqual(watch.step(quiet, 116.0), {"микрофон": True})  # the microphone waits 15 seconds
        self.assertEqual(watch.step(quiet, 161.0), {"микрофон": True, "собеседники": True})  # the others a minute
        self.assertEqual(
            watch.step({"микрофон": 5000, "собеседники": 0}, 162.0), {"микрофон": False, "собеседники": True}
        )
        self.assertEqual(watch.step(quiet, 170.0), {"собеседники": True})  # quiet again: its count starts over
        self.assertEqual(watch.step({}, 171.0), {"микрофон": False, "собеседники": False})  # a track not recorded

    def test_what_to_check(self):
        self.assertIn("программа встречи", silence.text("собеседники", set(), ""))
        self.assertIn("Zoom", silence.text("собеседники", {"zoom.exe"}, ""))
        self.assertNotIn("Bluetooth", silence.text("микрофон", set(), "Микрофон (Realtek)"))
        self.assertIn("Bluetooth", silence.text("микрофон", set(), "Headset (Hands-Free AG Audio)"))


if __name__ == "__main__":
    unittest.main()
