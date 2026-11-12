# SPDX-License-Identifier: GPL-3.0-only
"""Builds the portable version: updates the dist\\QG Knotes folder in place (the program is run from there) and puts
the archive dist\\QG-Knotes-<version>.zip; with Inno Setup installed, also the installer
dist\\QG-Knotes-<version>-setup.exe.

Run: .venv\\Scripts\\python.exe build.py
One folder, not one file: faster start, no unpacking into a temporary folder, fewer false
antivirus alarms. There is no code signature, so SmartScreen asks for confirmation on the first start.
"""

import importlib.metadata
import os
import pathlib
import shutil
import struct
import subprocess
import sys
import time
import winreg

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QLockFile
from PySide6.QtWidgets import QApplication

from knotes import VERSION, instance
from knotes.ui import style

ROOT = pathlib.Path(__file__).parent
LOCK = ROOT / "requirements.lock"  # exact library versions and hashes: a tampered package does not get into the build
BUILD = ROOT / "build"
DONE = BUILD / "готово"
NAME = "QG Knotes"
RESULT = ROOT / "dist"
PROGRAM = RESULT / NAME
SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
WAIT_EXIT_MS = 90_000  # the program saves a recording before exiting, up to a minute
RENAME_TRIES = 30  # a closed program needs time to release its files: tries half a second apart
INNO_SCRIPT = ROOT / "installer" / "qg-knotes.iss"
INNO_SITE = "https://jrsoftware.org/isdl.php"
INNO_VERSIONS = ("7", "6")
INNO_DEFAULT_ROOTS = (("LOCALAPPDATA", "Programs"), ("ProgramFiles", ""), ("ProgramFiles(x86)", ""))
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
REGISTRY_VIEWS = (
    (winreg.HKEY_CURRENT_USER, 0),
    (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
    (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
)
# _sherpa_onnx.pyd takes only onnxruntime.dll from its folder: the C and C++ wrappers for other languages are not needed
SHERPA_EXTRA = ("sherpa-onnx-c-api.dll", "sherpa-onnx-cxx-api.dll")
# Python embeds an older VC runtime in _internal; Windows loads it before PySide6's and QtWidgets fails with
# "The specified procedure could not be found". Overwrite with the copies that ship next to Qt.
CRT_FROM_QT = (
    "VCRUNTIME140.dll",
    "VCRUNTIME140_1.dll",
    "MSVCP140.dll",
    "MSVCP140_1.dll",
    "MSVCP140_2.dll",
)
# License of onnxruntime inside sherpa-onnx (MIT): the sherpa-onnx-core package does not include its text
ONNXRUNTIME = """MIT License

Copyright (c) Microsoft Corporation

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

EXE_VERSION = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=({ch}), prodvers=({ch}), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
  kids=[StringFileInfo([StringTable('041904B0', [
    StringStruct('CompanyName', 'JBskaro'),
    StringStruct('FileDescription', 'QG Knotes (Узелок) - запись и расшифровка встреч'),
    StringStruct('FileVersion', '{v}'),
    StringStruct('InternalName', 'QG Knotes'),
    StringStruct('OriginalFilename', 'QG Knotes.exe'),
    StringStruct('ProductName', 'QG Knotes'),
    StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [1049, 1200])])])
"""


def ico_icon(path: pathlib.Path) -> None:
    """ICO from PNGs of all sizes (format: header, directory, images)."""
    icon = style.tray_icon(False)
    pixmaps = []
    for size in SIZES:
        data = QByteArray()
        clipboard = QBuffer(data)
        clipboard.open(QIODevice.OpenModeFlag.WriteOnly)
        icon.pixmap(size, size).save(clipboard, "PNG")
        pixmaps.append((size, bytes(data.data())))
    header = struct.pack("<HHH", 0, 1, len(pixmaps))
    offset, contents = 6 + 16 * len(pixmaps), b""
    for size, png in pixmaps:
        contents += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    path.write_bytes(header + contents + b"".join(png for _, png in pixmaps))


def lighten(folder: pathlib.Path) -> None:
    """Removes from the build what widget windows do not need: software OpenGL and Qt translations other than
    Russian; from sherpa-onnx, the wrappers for other languages."""
    qt = folder / "_internal" / "PySide6"
    (qt / "opengl32sw.dll").unlink(missing_ok=True)
    for translator in (qt / "translations").glob("*.qm"):
        if not translator.stem.endswith("_ru"):
            translator.unlink()
    for extra in SHERPA_EXTRA:
        (folder / "_internal" / "sherpa_onnx" / "lib" / extra).unlink(missing_ok=True)


def match_qt_crt(folder: pathlib.Path) -> None:
    """Replaces the VC runtime in _internal with the versions from PySide6 so Qt is not loaded against Python's older
    CRT."""
    internal = folder / "_internal"
    qt = internal / "PySide6"
    for name in CRT_FROM_QT:
        source = qt / name
        if source.is_file():
            shutil.copy2(source, internal / name)


def licenses(folder: pathlib.Path) -> None:
    """Puts the license texts into _internal\\licenses, which an update replaces with the program: the program's own
    GPL-3.0, the third-party notices, LGPL-3.0 for Qt, sherpa-onnx (Apache-2.0) and onnxruntime (MIT)."""
    where_to = folder / "_internal" / "licenses"
    where_to.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "LICENSE", where_to / "LICENSE.txt")
    shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.md", where_to / "THIRD_PARTY_NOTICES.md")
    shutil.copyfile(ROOT / "LICENSES" / "LGPL-3.0-only.txt", where_to / "LGPL-3.0.txt")
    sherpa = next(file for file in importlib.metadata.distribution("sherpa-onnx").files or [] if file.name == "LICENSE")
    shutil.copyfile(sherpa.locate(), where_to / "sherpa-onnx.txt")
    (where_to / "onnxruntime.txt").write_text(ONNXRUNTIME, encoding="utf-8")


def _close_running(folder: pathlib.Path, exe: str) -> bool:
    """Asks the program from the folder to exit and waits until it has: False while it runs (it may be asking
    whether to stop a recording)."""
    if not (folder / exe).exists():
        return True
    subprocess.run([str(folder / exe), "--quit"], check=False, timeout=30)
    lock = QLockFile(str(instance.lock_path()))
    if not lock.tryLock(WAIT_EXIT_MS):
        return False
    lock.unlock()
    return True


def _rename(source: pathlib.Path, target: pathlib.Path) -> bool:
    for _ in range(RENAME_TRIES):
        try:
            source.rename(target)
            return True
        except PermissionError:
            time.sleep(0.5)
    return False


def install(build: pathlib.Path, program: pathlib.Path = PROGRAM) -> bool:
    """The new build is copied next to the program, then takes its place by renaming: a folder half deleted under
    a program that did not exit is never left behind."""
    new_one, old_one = program.with_name(program.name + ".новая"), program.with_name(program.name + ".старая")
    for leftover in (new_one, old_one):
        shutil.rmtree(leftover, ignore_errors=True)
    shutil.copytree(build, new_one)
    if not _close_running(program, f"{NAME}.exe"):
        print(
            f"The program is still running (it may be asking whether to stop a recording): close it and build again. "
            f"The new build is in {new_one}"
        )
        return False
    if program.exists() and not _rename(program, old_one):
        print(
            f"The files of {program} are still in use: close what holds them and build again. The new build is in "
            f"{new_one}"
        )
        return False
    if not _rename(new_one, program):  # an antivirus still checks the new files: the old program comes back
        _rename(old_one, program)
        print(f"The new build could not take the place of the program: build again. The new build is in {new_one}")
        return False
    shutil.rmtree(old_one, ignore_errors=True)
    return True


def mismatches() -> list[str]:
    """Libraries whose versions in the build environment do not match requirements.lock."""
    result = []
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "==" not in line:  # --hash lines belong to the package above
            continue
        name, needed = line.split()[0].split("==", 1)
        try:
            exists = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            exists = "missing"
        if exists != needed:
            result.append(f"{name}: {exists} instead of {needed}")
    return result


def registered_folders(key: str) -> list[pathlib.Path]:
    """InstallLocation of an Inno Setup uninstall entry, per-user and for all users."""
    folders = []
    for root, view in REGISTRY_VIEWS:
        try:
            with winreg.OpenKey(root, rf"{UNINSTALL_KEY}\{key}", 0, winreg.KEY_READ | view) as entry:
                folders.append(pathlib.Path(winreg.QueryValueEx(entry, "InstallLocation")[0]))
        except OSError:  # not installed for this user or for all users
            continue
    return folders


def inno_folders() -> list[pathlib.Path]:
    """Folders where Inno Setup may be installed, newest version first."""
    folders = []
    for version in INNO_VERSIONS:
        folders += registered_folders(f"Inno Setup {version}_is1")
        folders += [
            pathlib.Path(os.environ[name]) / sub / f"Inno Setup {version}"
            for name, sub in INNO_DEFAULT_ROOTS
            if name in os.environ
        ]
    return folders


def find_iscc() -> pathlib.Path | None:
    """The Inno Setup compiler from PATH or an installed Inno Setup."""
    on_path = shutil.which("ISCC")
    candidates = [pathlib.Path(on_path)] if on_path else []
    candidates += [folder / "ISCC.exe" for folder in inno_folders()]
    return next((path for path in candidates if path.is_file()), None)


def setup_path() -> pathlib.Path:
    return RESULT / f"QG-Knotes-{VERSION}-setup.exe"


def build_installer(source: pathlib.Path, icon: pathlib.Path) -> pathlib.Path | None:
    """Compiles the installer from the PyInstaller folder; None if Inno Setup is not installed."""
    iscc = find_iscc()
    if iscc is None:
        print(f"Inno Setup is not installed: no installer, only the zip. Inno Setup is free: {INNO_SITE}")
        return None
    command = [
        str(iscc),
        "/Q",
        f"/DAppVersion={VERSION}",
        f"/DSourceDir={source}",
        f"/DIconFile={icon}",
        f"/O{RESULT}",
        str(INNO_SCRIPT),
    ]
    result = subprocess.run(command, check=False)
    if result.returncode:
        raise SystemExit(f"The installer was not built: Inno Setup returned {result.returncode}")
    return setup_path()


def main() -> int:
    others = mismatches()
    if others:
        print("Library versions differ from requirements.lock, build stopped:", "; ".join(others))
        return 1
    QApplication(sys.argv[:1])
    BUILD.mkdir(exist_ok=True)
    ico_icon(BUILD / "значок.ico")
    numbers_ = ", ".join((VERSION.split(".") + ["0"] * 4)[:4])
    (BUILD / "версия.txt").write_text(EXE_VERSION.format(ch=numbers_, v=VERSION), encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--onedir",
        "--name",
        NAME,
        "--icon",
        str(BUILD / "значок.ico"),
        "--version-file",
        str(BUILD / "версия.txt"),
        "--add-data",
        f"{ROOT / 'knotes' / 'providers.json'};knotes",
        "--add-data",
        f"{ROOT / 'knotes' / 'models.json'};knotes",
        "--add-data",
        f"{ROOT / 'knotes' / 'minutes_prompt.md'};knotes",
        "--add-data",
        f"{ROOT / 'knotes' / 'prep_prompt.md'};knotes",
        "--add-data",
        f"{ROOT / 'knotes' / 'meeting_kinds.json'};knotes",
        "--add-data",
        f"{ROOT / 'knotes' / 'minutes_languages.json'};knotes",
        "--add-data",
        f"{ROOT / 'knotes' / 'translations'};knotes/translations",
        "--collect-binaries",
        "imageio_ffmpeg",
        "--copy-metadata",
        "keyring",
        "--collect-binaries",
        "sherpa_onnx",  # onnxruntime.dll next to _sherpa_onnx.pyd
        "--hidden-import",
        "keyring.backends.Windows",
        "--exclude-module",
        "tkinter",
        "--distpath",
        str(DONE),
        "--workpath",
        str(BUILD / "pyinstaller"),
        "--specpath",
        str(BUILD),
        str(ROOT / "launch.pyw"),
    ]
    result = subprocess.run(command, check=False)
    if result.returncode:
        return result.returncode
    lighten(DONE / NAME)
    match_qt_crt(DONE / NAME)
    licenses(DONE / NAME)
    archive = shutil.make_archive(str(RESULT / f"QG-Knotes-{VERSION}"), "zip", DONE, NAME)
    if not install(DONE / NAME):
        return 1
    setup = build_installer(DONE / NAME, BUILD / "значок.ico")
    print("Done:", PROGRAM / f"{NAME}.exe", "and", archive, *(["and", setup] if setup else []))
    return 0


if __name__ == "__main__":
    sys.exit(main())
