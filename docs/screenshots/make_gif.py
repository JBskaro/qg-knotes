# SPDX-License-Identifier: GPL-3.0-only
"""A GIF from numbered PNG frames: one palette for the whole clip, and repeated frames merged into one longer frame,
so a 15-second clip of a mostly still window stays a few hundred kilobytes (ffmpeg wrote every frame in full, 9 MB).

Usage: python make_gif.py <frames folder> <out.gif> <milliseconds per frame>. Needs Pillow.
"""

import pathlib
import sys

from PIL import Image

SAMPLES = 4  # frames spread over the clip that the palette is taken from


def main(folder: pathlib.Path, out: pathlib.Path, duration: int) -> None:
    frames = [Image.open(path).convert("RGB") for path in sorted(folder.glob("*.png"))]
    width, height = frames[0].size
    sample = Image.new("RGB", (width, height * SAMPLES))
    for row in range(SAMPLES):
        sample.paste(frames[row * (len(frames) - 1) // (SAMPLES - 1)], (0, height * row))
    palette = sample.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    paletted = [image.quantize(palette=palette, dither=Image.Dither.NONE) for image in frames]
    paletted[0].save(out, save_all=True, append_images=paletted[1:], duration=duration, loop=0, disposal=1)


if __name__ == "__main__":
    main(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), int(sys.argv[3]))
