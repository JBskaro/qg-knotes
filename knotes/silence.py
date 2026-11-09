# SPDX-License-Identifier: GPL-3.0-only
"""Silence on a recording track: a muted microphone, a meeting program that plays its sound to another device. A track
quiet longer than its limit gets a warning on the recording bar; once it sounds again, the warning goes."""

from knotes import settings
from knotes.language import tr

LIMITS = {"микрофон": (300, 15), "собеседники": (30, 60)}  # track: (peak below this, for how many seconds)


class Watch:
    """Silence of the tracks of one recording."""

    def __init__(self):
        self._quiet_since: dict[str, float] = {}

    def step(self, levels: dict[str, int], now: float) -> dict[str, bool]:
        """Peaks of the tracks now -> True: warn, the track is silent past its limit; False: it sounds, no warning.
        A track quiet for less than its limit is left as it is."""
        result = {}
        for track, (threshold, seconds) in LIMITS.items():
            peak = levels.get(track)
            if peak is None or peak >= threshold:
                self._quiet_since.pop(track, None)
                result[track] = False
            elif now - self._quiet_since.setdefault(track, now) > seconds:
                result[track] = True
        return result


def text(track: str, programs: set[str], microphone: str) -> str:
    """The warning; programs: the meeting programs whose toast started the recording, microphone: its device."""
    if track == "собеседники" and not programs:
        return tr(
            "No sound from other participants for a minute. If they're talking, check where the meeting app plays "
            "its audio."
        )
    if track == "собеседники":
        return tr(
            "No sound from other participants for a minute. If they're talking, check where {program} plays its audio.",
            program=settings.program_name(next(iter(programs))),
        )
    reason = (
        " " + tr("A Bluetooth headset in call mode sounds worse; a wired microphone is better.")
        if "hands-free" in microphone.lower()
        else ""
    )
    return tr("Can't hear the microphone: check that it's on.") + reason
