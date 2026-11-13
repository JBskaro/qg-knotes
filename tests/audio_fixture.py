# SPDX-License-Identifier: GPL-3.0-only

import pathlib

import numpy as np

from knotes import recognition


def write_flac(audio: np.ndarray, path: pathlib.Path) -> None:
    raw_items = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    recognition._ffmpeg(
        "-y",
        "-f",
        "s16le",
        "-ar",
        str(recognition.SAMPLE_RATE),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-c:a",
        "flac",
        str(path),
        input_=raw_items,
    )
