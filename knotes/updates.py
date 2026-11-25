# SPDX-License-Identifier: GPL-3.0-only
"""Program updates from GitHub releases: whether a newer version is out, downloading its file with the checksum and
installing it - the installer runs quietly, the portable copy swaps its program files after the old copy exits."""

import ctypes
import filecmp
import dataclasses
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import zipfile
from collections.abc import Callable

from knotes import RUNTIME, VERSION, models, providers, system, transcript
from knotes import settings
from knotes.language import tr

REPOSITORY = "JBskaro/qg-knotes"
LATEST = "https://api.github.com/repos/{repository}/releases/latest"
EXE = "QG Knotes.exe"
PREVIOUS_RUNTIME = ".qg-previous-runtime"
UNINSTALLER = "unins000.exe"  # Inno Setup leaves it next to the installed program
SETUP_ARGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LAUNCH=1"]  # /LAUNCH=1: start the new version
SWAP_FLAG = "--update-swap"  # the new copy replaces the old one: <new folder> <old folder> <old pid>
WAIT_EXIT_S = 60
DETACHED = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
MB_ERROR, MB_RETRY, RETRY = 0x10, 0x35, 4  # MB_ICONERROR; MB_RETRYCANCEL with MB_ICONWARNING; IDRETRY
log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    sha256: str  # empty when GitHub gave no digest: such a file is not downloaded


@dataclasses.dataclass(frozen=True)
class Release:
    version: str
    notes: str
    page: str
    assets: tuple[Asset, ...]


def version_tuple(text: str) -> tuple[int, ...]:
    found = re.fullmatch(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(part) for part in found.group(1).split(".")) if found else ()


def is_newer(version: str, current: str = VERSION) -> bool:
    return bool(version_tuple(version)) and version_tuple(version) > version_tuple(current)


def _asset(data: dict) -> Asset:
    digest = str(data.get("digest") or "")
    sha256 = digest.removeprefix("sha256:").lower()
    return Asset(
        str(data["name"]),
        str(data["browser_download_url"]),
        int(data.get("size") or 0),
        sha256 if digest.startswith("sha256:") and models.SHA256.fullmatch(sha256) else "",
    )


def latest(repository: str = REPOSITORY) -> Release | None:
    """The latest release; None when the repository has none or is not public."""
    code, text = providers.fetch(LATEST.format(repository=repository), 30)
    if code == 404:
        return None
    if code != 200:
        raise providers.ProviderError(
            tr("GitHub did not answer about a new version, code {code}. Try again later.", code=code)
        )
    try:
        data = json.loads(text)
        assets = tuple(_asset(item) for item in data.get("assets", []) if item.get("browser_download_url"))
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise providers.ProviderError(tr("GitHub sent an unclear answer about a new version")) from error
    return Release(
        str(data.get("tag_name", "")).lstrip("v"), str(data.get("body") or ""), str(data.get("html_url", "")), assets
    )


def program_folder() -> pathlib.Path:
    return pathlib.Path(sys.executable).parent


def frozen() -> bool:
    """Running from the build: only the build can update itself."""
    return bool(getattr(sys, "frozen", False))


def installed(folder: pathlib.Path | None = None) -> bool:
    """Installed by the installer, not unpacked from the archive."""
    return ((folder or program_folder()) / UNINSTALLER).exists()


def asset_for(release: Release, by_setup: bool) -> Asset | None:
    suffix = "-setup.exe" if by_setup else ".zip"
    name = f"QG-Knotes-{release.version.removeprefix('v')}{suffix}"
    return next((asset for asset in release.assets if asset.name == name and asset.size > 0), None)


def updates_folder() -> pathlib.Path:
    return settings.log_folder() / "updates"


def download(
    asset: Asset, progress: Callable[[int, int], None] | None = None, cancel: threading.Event | None = None
) -> pathlib.Path:
    """The release file, checked by the sha256 GitHub gave for it; a file without one is not downloaded: its size
    alone would not reveal a swapped file."""
    verify_checksum(asset)
    return models.download_file(
        updates_folder(), models.File(asset.name, asset.size, asset.sha256, (asset.url,)), progress, cancel
    )


def verify_checksum(asset: Asset) -> None:
    if not models.SHA256.fullmatch(asset.sha256):
        log.warning("update %s: GitHub gave no sha256", asset.name)
        raise providers.ProviderError(
            tr(
                "GitHub did not give a checksum for {name}, so the update can't be checked and was not downloaded. Try "
                "again later or download it from the release page.",
                name=asset.name,
            )
        )


def run_setup(setup: pathlib.Path) -> None:
    """The installer closes this copy itself (--quit) and starts the new one when it is done."""
    subprocess.Popen([str(setup), *SETUP_ARGS], creationflags=DETACHED, close_fds=True)


def stage_portable(archive: pathlib.Path) -> pathlib.Path:
    """Unpacks the new portable copy next to its archive; the folder with its exe."""
    staging = archive.with_suffix("")
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(archive) as packed:
        for member in packed.infolist():
            path = pathlib.PurePosixPath(member.filename.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts or ":" in member.filename:
                raise providers.ProviderError(
                    tr("The update archive is broken: it has paths outside the program folder")
                )
        packed.extractall(staging)
    exe = next(staging.glob(f"*/{EXE}"), None) or next(staging.glob(EXE), None)
    if exe is None:
        raise providers.ProviderError(tr("The update archive has no {exe}", exe=EXE))
    return exe.parent


def start_swap(new_folder: pathlib.Path, old_folder: pathlib.Path | None = None) -> None:
    """The new copy waits for this one to exit, puts its files in place and starts from there."""
    subprocess.Popen(
        [
            str(new_folder / EXE),
            SWAP_FLAG,
            str(new_folder),
            str(old_folder or program_folder()),
            str(os.getpid()),
            pathlib.Path(getattr(sys, "_MEIPASS", "_internal")).name,
        ],
        creationflags=DETACHED,
        close_fds=True,
    )


def wait_for_exit(pid: int, seconds: float = WAIT_EXIT_S) -> bool:
    """True once the process is gone (or never was)."""
    synchronize = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return True
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(handle, int(seconds * 1000)) == 0
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _same_runtime(first: pathlib.Path, second: pathlib.Path) -> bool:
    for path in first.rglob("*"):
        other = second / path.relative_to(first)
        if path.is_file() and (not other.is_file() or not filecmp.cmp(path, other, shallow=False)):
            return False
    return {p.relative_to(first) for p in first.rglob("*")} == {p.relative_to(second) for p in second.rglob("*")}


def swap(new_folder: pathlib.Path, old_folder: pathlib.Path, previous: str = "_internal") -> None:
    """The executable is the commit point. Both runtimes remain usable until the new GUI starts."""
    source, target = new_folder / RUNTIME, old_folder / RUNTIME
    if not source.is_dir() or not (new_folder / EXE).is_file():
        raise FileNotFoundError(tr("The new copy of the program lacks the needed files"))
    with tempfile.TemporaryDirectory(prefix=".qg-update-", dir=old_folder) as temp:
        stage = pathlib.Path(temp)
        shutil.copy2(new_folder / EXE, stage / EXE)
        if not target.exists():
            shutil.copytree(source, stage / RUNTIME)
            (stage / RUNTIME).rename(target)
        elif not _same_runtime(source, target):
            raise OSError(tr("A different runtime already exists in {folder}.", folder=target))
        if previous != RUNTIME and re.fullmatch(r"_internal|_qg_runtime_[0-9]+(?:\.[0-9]+)*", previous):
            transcript.write_text(old_folder / PREVIOUS_RUNTIME, previous)
        transcript.replace(stage / EXE, old_folder / EXE)


def _message(text: str, flags: int = MB_ERROR) -> int:
    """A Windows message box: this copy has no Qt windows. Returns the pressed button."""
    return ctypes.windll.user32.MessageBoxW(None, text, "QG Knotes", flags)


def _others_closed(old_folder: pathlib.Path) -> bool:
    """While other processes run from the program folder, its files cannot be replaced: ask to close them."""
    while busy := system.processes_in(old_folder):
        log.warning("update: processes %s run from %s", busy, old_folder)
        text = tr(
            "The update can't replace the program files while other programs use them. Close Claude Desktop (it "
            "starts QG Knotes for its tools) and other QG Knotes windows, then press Retry."
        )
        if _message(text, MB_RETRY) != RETRY:
            return False
    return True


def finish_swap(arguments: list[str]) -> int:
    """Runs in the new copy, without Qt windows: wait, swap, start the updated program."""
    new_folder, old_folder, pid = pathlib.Path(arguments[0]), pathlib.Path(arguments[1]), int(arguments[2])
    if not wait_for_exit(pid):
        log.error("update: the previous copy did not exit in %d s", WAIT_EXIT_S)
        _message(
            tr(
                "The update was not installed: the running program did not close within a minute. Close it and update "
                "again."
            )
        )
        return 1
    if not _others_closed(old_folder):
        subprocess.Popen([str(old_folder / EXE)], creationflags=DETACHED, close_fds=True)
        return 1
    try:
        swap(new_folder, old_folder, arguments[3] if len(arguments) > 3 else "_internal")
    except OSError:
        log.exception("the update did not install")
        _message(
            tr(
                "The update was not installed: the program files are in use. The previous version is in place, start "
                "it and update again."
            )
        )
        return 1
    subprocess.Popen([str(old_folder / EXE)], creationflags=DETACHED, close_fds=True)
    return 0


def cleanup() -> None:
    """The downloaded files and the unpacked copy are not needed once the new version runs."""
    program = program_folder()
    marker = program / PREVIOUS_RUNTIME
    if pathlib.Path(getattr(sys, "_MEIPASS", "")) == program / RUNTIME and marker.is_file():
        try:
            old = marker.read_text(encoding="utf-8")
            if old != RUNTIME and re.fullmatch(r"_internal|_qg_runtime_[0-9]+(?:\.[0-9]+)*", old):
                runtime = program / old
                if runtime.is_dir() and not runtime.is_junction() and not runtime.is_symlink():
                    shutil.rmtree(runtime)
            marker.unlink()
        except OSError as error:
            log.warning("previous runtime cleanup: %s", error)
    folder = updates_folder()
    if folder.exists() and folder not in program_folder().parents:
        shutil.rmtree(folder, ignore_errors=True)
