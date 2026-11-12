# Third-party notices

QG Knotes is licensed under the GNU General Public License version 3 (see `LICENSE`). The Windows build bundles the
components below; each keeps its own license. Python package versions are pinned in `requirements.lock`;
the DirectML-enabled sherpa-onnx binary is built from the pinned 2.0.0 source in `.github/workflows/directml-wheel.yml`.

## Bundled in the program

| Component | Version | License | Source |
|---|---|---|---|
| Python runtime | 3.13 | PSF-2.0 | https://www.python.org |
| PySide6 (Qt for Python) and shiboken6 | 6.12.0 | LGPL-3.0-only (used under the LGPL option) | https://doc.qt.io/qtforpython |
| Qt libraries shipped with PySide6 | 6.12 | LGPL-3.0-only | https://www.qt.io/download-open-source |
| sherpa-onnx, sherpa-onnx-core | 2.0.0 | Apache-2.0 | https://github.com/k2-fsa/sherpa-onnx |
| ONNX Runtime (inside sherpa-onnx) | | MIT | https://github.com/microsoft/onnxruntime |
| Microsoft DirectML redistributable | 1.15.0 | Microsoft DirectML Software License Terms; see `LICENSES/DirectML-LICENSE.txt` and `LICENSES/DirectML-ThirdPartyNotices.txt` | https://www.nuget.org/packages/Microsoft.AI.DirectML/1.15.0 |
| NumPy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 (bundled OpenBLAS: BSD-3-Clause; GCC runtime: GPL-3.0 with the GCC Runtime Library Exception) | https://numpy.org |
| PyAudioWPatch (with PortAudio) | 0.2.12.9 | Apache-2.0 (PortAudio: MIT) | https://github.com/s0d3s/PyAudioWPatch |
| pycaw | 20260927 | MIT | https://github.com/AndreMiras/pycaw |
| comtypes | 1.4.17 | MIT | https://github.com/enthought/comtypes |
| psutil | 7.2.2 | BSD-3-Clause | https://github.com/giampaolo/psutil |
| keyring | 25.7.0 | MIT | https://github.com/jaraco/keyring |
| jaraco.classes, jaraco.context, jaraco.functools | 3.4.0, 6.1.2, 4.6.0 | MIT | https://github.com/jaraco |
| more-itertools | 11.2.1 | MIT | https://github.com/more-itertools/more-itertools |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause | https://github.com/enthought/pywin32-ctypes |
| imageio-ffmpeg | 0.6.0 | BSD-2-Clause | https://github.com/imageio/imageio-ffmpeg |
| FFmpeg 7.1 "essentials" build by gyan.dev (shipped by imageio-ffmpeg) | 7.1 | GPL-3.0-or-later (built with `--enable-gpl --enable-version3`) | https://ffmpeg.org, https://www.gyan.dev/ffmpeg/builds/ |
| PyInstaller bootloader | 6.22.3 | GPL-2.0-or-later with the bootloader exception | https://pyinstaller.org |

The Windows build carries these texts in `_internal\licenses`: the GPL-3.0 of the program, this file, the
LGPL-3.0 for Qt and PySide6, and the licenses of sherpa-onnx, ONNX Runtime and DirectML.

### FFmpeg source code

The FFmpeg binary is a GPL build. Its corresponding source code is the FFmpeg 7.1 release from https://ffmpeg.org
with the configuration printed by `ffmpeg -version`; gyan.dev publishes the build and the versions of the libraries it
links. On request, the maintainers of QG Knotes provide the same source code for at least three years after the
release that shipped it.

### Qt and PySide6

Qt and PySide6 are used under the LGPL-3.0. They are linked dynamically: the Qt and PySide6 libraries in the
program folder can be replaced with compatible builds of your own. Their source code is available from the Qt
Project at https://download.qt.io/official_releases/.

### Installer

`QG-Knotes-<version>-setup.exe` and the uninstaller `unins000.exe` it leaves next to the installed program are made
with Inno Setup (https://jrsoftware.org/isinfo.php), Copyright (C) 1997-2026 Jordan Russell, portions Copyright (C)
2000-2026 Martijn Laan, under the Inno Setup License (https://jrsoftware.org/files/is/license.txt).

## Downloaded by the program at the user's request

Speech models are not part of the program; the user downloads them from their authors' pages when choosing local
speech recognition. Their licenses:

| Model | License | Authors | Link |
|---|---|---|---|
| GigaAM v3 | MIT | GigaChat Team (Sber) | https://github.com/salute-developers/GigaAM |
| Whisper large-v3-turbo | MIT | OpenAI | https://github.com/openai/whisper |
| Silero VAD | MIT | Silero Team | https://github.com/snakers4/silero-vad |
| pyannote segmentation 3.0 | MIT | CNRS (pyannote) | https://github.com/pyannote/pyannote-audio |
| WeSpeaker ResNet34 (VoxCeleb) | CC-BY-4.0 | WeSpeaker | https://github.com/wenet-e2e/wespeaker |

The models are converted to ONNX by the sherpa-onnx project (Apache-2.0). A model the user adds from Hugging Face
keeps the license stated on its page.

## Used only to build and test

PyInstaller (GPL-2.0-or-later with the bootloader exception), ruff (MIT), vulture (MIT), pylint (GPL-2.0-or-later),
coverage (Apache-2.0). They are not part of the program.
