# SPDX-License-Identifier: GPL-3.0-only
"""Recording bar: loudness in bars, warnings, tooltips by the settings."""

import unittest

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from knotes.ui import recording_bar

app = QApplication.instance() or QApplication([])


class TestLoudness(unittest.TestCase):
    def test_quiet_speech_visible_loud_not_clipped(self):
        self.assertEqual(recording_bar.loudness_share(0), 0.0)
        self.assertLess(recording_bar.loudness_share(300), recording_bar.QUIET)  # room noise: grey bars
        self.assertGreater(recording_bar.loudness_share(1500), 0.3)  # quiet speech
        self.assertLess(recording_bar.loudness_share(12000), 1.0)  # normal speech at the microphone
        self.assertEqual(recording_bar.loudness_share(32767), 1.0)

    def test_up_fast_down_smooth(self):
        upward = recording_bar.step_to(0.0, 1.0)
        self.assertGreater(upward, 0.5)
        shown, frame_count = 1.0, 0
        while shown > 0:
            shown, frame_count = recording_bar.step_to(shown, 0.0), frame_count + 1
        self.assertGreater(
            frame_count * recording_bar.FRAME_MS, 400
        )  # bars do not drop to zero between readings every 0.2 s
        self.assertLess(frame_count * recording_bar.FRAME_MS, 1500)


class TestBar(unittest.TestCase):
    def setUp(self):
        self.bar = recording_bar.RecordingBar()
        self.addCleanup(self.bar.deleteLater)

    def test_important_warning_expands_once(self):
        """A silent microphone is warned about on every tick: the bar expands when the warning appears, and the
        user can collapse it again."""
        self.bar.collapse(True)
        self.bar.warn("микрофон", "Не слышно микрофон", expand=True)
        self.assertFalse(self.bar.is_collapsed)
        self.bar.collapse(True)
        for _ in range(3):
            self.bar.warn("микрофон", "Не слышно микрофон", expand=True)
        self.assertTrue(self.bar.is_collapsed)
        self.bar.remove("микрофон")
        self.bar.warn("микрофон", "Не слышно микрофон", expand=True)  # a new warning after the sound came back
        self.assertFalse(self.bar.is_collapsed)

    def test_dragged_anywhere_and_its_place_remembered(self):
        """The bar follows the mouse held on it and reports where it was dropped, to open there next time."""
        self.bar.move(100, 100)
        places: list[QPoint] = []
        self.bar.moved.connect(places.append)
        press, move = QPointF(110, 105), QPointF(210, 165)
        left, none = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
        for kind, at, buttons in (
            (QEvent.Type.MouseButtonPress, press, left),
            (QEvent.Type.MouseMove, move, left),
            (QEvent.Type.MouseButtonRelease, move, Qt.MouseButton.NoButton),
        ):
            event = QMouseEvent(kind, QPointF(10, 5), at, left, buttons, none)
            QApplication.sendEvent(self.bar, event)
        self.assertEqual((self.bar.pos(), places), (QPoint(200, 160), [QPoint(200, 160)]))

    def test_notepad_hidden_from_participants_only_when_it_is(self):
        self.assertIn("не видят", self.bar._note.toolTip())
        self.bar.hide_from_capture(False)
        self.assertNotIn("не видят", self.bar._note.toolTip())
        shown = recording_bar.RecordingBar(hide_from_capture=False, notes_hotkey="Ctrl+Alt+N")
        self.addCleanup(shown.deleteLater)
        self.assertNotIn("не видят", shown._note.toolTip())
        self.assertIn("Ctrl+Alt+N", shown._note.toolTip())


if __name__ == "__main__":
    unittest.main()
