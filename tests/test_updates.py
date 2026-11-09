# SPDX-License-Identifier: GPL-3.0-only
"""Updates from GitHub releases: versions, the latest release and its files, which file fits the installed or the
portable copy, the download through the models downloader, the quiet installer run, unpacking the portable copy
without paths outside its folder and swapping the program files with a way back. Nothing goes to the network."""

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

from knotes import models, providers, updates

RELEASE = {
    "tag_name": "v0.20.0",
    "body": "- Протокол своим помощником ИИ.\n- Обновление с GitHub.",
    "html_url": "https://github.com/JBskaro/qg-knotes/releases/tag/v0.20.0",
    "assets": [
        {
            "name": "QG-Knotes-0.20.0.zip",
            "size": 80,
            "digest": "sha256:" + "a" * 64,
            "browser_download_url": "https://github.com/x/QG-Knotes-0.20.0.zip",
        },
        {
            "name": "QG-Knotes-0.20.0-setup.exe",
            "size": 70,
            "digest": None,
            "browser_download_url": "https://github.com/x/QG-Knotes-0.20.0-setup.exe",
        },
    ],
}


class TestRelease(unittest.TestCase):
    def test_versions(self):
        self.assertTrue(updates.is_newer("0.20.0", "0.19.0"))
        self.assertTrue(updates.is_newer("v0.19.10", "0.19.9"))
        self.assertFalse(updates.is_newer("0.19.0", "0.19.0"))
        self.assertFalse(updates.is_newer("бета", "0.19.0"))

    def test_latest_release_and_its_files(self):
        with mock.patch.object(providers, "fetch", return_value=(200, json.dumps(RELEASE))) as fetch:
            release = updates.latest()
        self.assertIn("api.github.com/repos/JBskaro/qg-knotes/releases/latest", fetch.call_args.args[0])
        self.assertEqual(
            (release.version, release.notes.splitlines()[0]), ("0.20.0", "- Протокол своим помощником ИИ.")
        )
        archive = updates.asset_for(release, by_setup=False)
        self.assertEqual((archive.name, archive.sha256), ("QG-Knotes-0.20.0.zip", "a" * 64))
        self.assertEqual(updates.asset_for(release, by_setup=True).sha256, "")

    def test_no_releases_or_private(self):
        with mock.patch.object(providers, "fetch", return_value=(404, "")):
            self.assertIsNone(updates.latest())
        with mock.patch.object(providers, "fetch", return_value=(500, "")), self.assertRaises(providers.ProviderError):
            updates.latest()

    def test_download_via_models_downloader(self):
        release = updates.Release("0.20.0", "", "", tuple(updates._asset(d) for d in RELEASE["assets"]))
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(updates, "updates_folder", return_value=pathlib.Path(tmp)),
            mock.patch.object(models, "download_file", return_value=pathlib.Path(tmp) / "x") as download,
        ):
            updates.download(updates.asset_for(release, by_setup=False))
        place, file = download.call_args.args[:2]
        self.assertEqual(
            (place, file.name, file.size, file.sha256, file.addresses),
            (pathlib.Path(tmp), "QG-Knotes-0.20.0.zip", 80, "a" * 64, ("https://github.com/x/QG-Knotes-0.20.0.zip",)),
        )

    def test_file_without_checksum_not_downloaded(self):
        """Without GitHub's sha256 only the size could be checked: a swapped file of the same size would get in."""
        release = updates.Release("0.20.0", "", "", tuple(updates._asset(d) for d in RELEASE["assets"]))
        broken = updates._asset(RELEASE["assets"][0] | {"digest": "sha256:не-хеш"})
        for asset in (updates.asset_for(release, by_setup=True), broken):
            with (
                self.subTest(asset=asset.name),
                mock.patch.object(models, "download_file") as download,
                self.assertLogs(updates.log, "WARNING"),
                self.assertRaises(providers.ProviderError) as error,
            ):
                updates.download(asset)
            download.assert_not_called()
            self.assertIn("контрольной суммы", str(error.exception))


class TestInstall(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = pathlib.Path(self._tmp.name)

    def test_silent_installer(self):
        with mock.patch.object(updates.subprocess, "Popen") as startup:
            updates.run_setup(self.folder / "QG-Knotes-0.20.0-setup.exe")
        self.assertEqual(startup.call_args.args[0][1:], ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LAUNCH=1"])

    def test_installed_copy_by_uninstaller(self):
        self.assertFalse(updates.installed(self.folder))
        (self.folder / "unins000.exe").write_bytes(b"")
        self.assertTrue(updates.installed(self.folder))

    def _archive(self, members: dict[str, bytes]) -> pathlib.Path:
        archive = self.folder / "QG-Knotes-0.20.0.zip"
        with zipfile.ZipFile(archive, "w") as z:
            for name, data in members.items():
                z.writestr(name, data)
        return archive

    def test_unpacking_portable(self):
        new_one = updates.stage_portable(
            self._archive({"QG Knotes/QG Knotes.exe": b"new", "QG Knotes/_internal/lib.dll": b"lib"})
        )
        self.assertEqual((new_one / "QG Knotes.exe").read_bytes(), b"new")

    def test_archive_with_outside_path_is_not_unpacked(self):
        with self.assertRaises(providers.ProviderError):
            updates.stage_portable(self._archive({"../evil.exe": b"x", "QG Knotes/QG Knotes.exe": b"new"}))
        self.assertFalse((self.folder.parent / "evil.exe").exists())

    def test_swap_program_files_keeps_others(self):
        old_one, new_one = self.folder / "старая", self.folder / "новая"
        for folder, label in ((old_one, b"old"), (new_one, b"new")):
            (folder / "_internal").mkdir(parents=True)
            (folder / "QG Knotes.exe").write_bytes(label)
            (folder / "_internal" / "lib.dll").write_bytes(label)
        (old_one / "_internal" / "only_old.dll").write_bytes(b"old")
        (old_one / "мои заметки.txt").write_bytes(b"mine")  # someone else's file next to the program
        updates.swap(new_one, old_one)
        self.assertEqual((old_one / "QG Knotes.exe").read_bytes(), b"new")
        self.assertEqual((old_one / "_internal" / "lib.dll").read_bytes(), b"new")
        self.assertFalse((old_one / "_internal" / "only_old.dll").exists())
        self.assertEqual((old_one / "мои заметки.txt").read_bytes(), b"mine")
        self.assertEqual(sorted(p.name for p in old_one.iterdir()), ["QG Knotes.exe", "_internal", "мои заметки.txt"])

    def test_failed_swap_restores_old(self):
        old_one, new_one = self.folder / "старая", self.folder / "новая"
        (old_one / "_internal").mkdir(parents=True)
        (old_one / "QG Knotes.exe").write_bytes(b"old")
        new_one.mkdir()  # the new copy has no program files
        with self.assertRaises(OSError):
            updates.swap(new_one, old_one)
        self.assertEqual((old_one / "QG Knotes.exe").read_bytes(), b"old")
        self.assertTrue((old_one / "_internal").is_dir())

    def _two_copies(self) -> tuple[pathlib.Path, pathlib.Path]:
        place = pathlib.Path(tempfile.mkdtemp(dir=self.folder))
        old_one, new_one = place / "старая", place / "новая"
        for folder, label in ((old_one, b"old"), (new_one, b"new")):
            (folder / "_internal").mkdir(parents=True)
            (folder / "QG Knotes.exe").write_bytes(label)
            (folder / "_internal" / "lib.dll").write_bytes(label)
        return old_one, new_one

    def _old_in_place(self, old_one: pathlib.Path) -> None:
        self.assertEqual(sorted(p.name for p in old_one.iterdir()), ["QG Knotes.exe", "_internal"])
        self.assertEqual((old_one / "QG Knotes.exe").read_bytes(), b"old")
        self.assertEqual((old_one / "_internal" / "lib.dll").read_bytes(), b"old")

    def test_folder_held_by_other_process_restores_exe(self):
        """The exe is moved aside, the _internal folder is held (Claude Desktop runs "QG Knotes.exe --mcp"): the exe
        comes back, the program is not lost."""
        old_one, new_one = self._two_copies()
        real = updates._replace

        def replace(path: pathlib.Path, backup: pathlib.Path) -> None:
            if path.name == "_internal":
                raise PermissionError(5, "Access is denied", str(path))
            real(path, backup)

        with (
            mock.patch.object(updates, "_replace", replace),
            self.assertLogs(updates.log, "ERROR"),
            self.assertRaises(PermissionError),
        ):
            updates.swap(new_one, old_one)
        self._old_in_place(old_one)

    def test_failed_copy_restores_both(self):
        old_one, new_one = self._two_copies()
        with (
            mock.patch.object(updates.shutil, "copytree", side_effect=OSError(28, "No space left on device")),
            self.assertLogs(updates.log, "ERROR"),
            self.assertRaises(OSError),
        ):
            updates.swap(new_one, old_one)
        self._old_in_place(old_one)

    def test_restore_failure_is_told(self):
        old_one, new_one = self._two_copies()
        with (
            mock.patch.object(updates.shutil, "copytree", side_effect=OSError(28, "No space left on device")),
            mock.patch.object(pathlib.Path, "rename", side_effect=PermissionError(5, "Access is denied")),
            mock.patch.object(updates, "_replace", lambda path, backup: path.replace(backup)),
            self.assertLogs(updates.log, "ERROR"),
            self.assertRaises(updates.RestoreError) as error,
        ):
            updates.swap(new_one, old_one)
        self.assertIn(".old", str(error.exception))

    def _finish(self, *, exited: bool = True, busy: list[list[int]] | None = None, answer: int = 2) -> tuple:
        old_one, new_one = self._two_copies()
        with (
            mock.patch.object(updates, "wait_for_exit", return_value=exited),
            mock.patch.object(updates.system, "processes_in", side_effect=busy or [[]]),
            mock.patch.object(updates, "_message", return_value=answer) as message,
            mock.patch.object(updates.subprocess, "Popen") as startup,
        ):
            code = updates.finish_swap([str(new_one), str(old_one), "123"])
        texts = [c.args[0] for c in message.call_args_list]
        return code, texts, startup.call_count, (old_one / "QG Knotes.exe").read_bytes()

    def test_finish_swap_installs_and_starts(self):
        self.assertEqual(self._finish(), (0, [], 1, b"new"))

    def test_old_copy_did_not_exit_is_told(self):
        with self.assertLogs(updates.log, "ERROR"):
            code, texts, started, exe = self._finish(exited=False)
        self.assertEqual((code, started, exe), (1, 0, b"old"))
        self.assertIn("не закрылась за минуту", texts[0])

    def test_files_in_use_ask_to_close_claude_desktop(self):
        with self.assertLogs(updates.log, "WARNING"):
            code, texts, started, exe = self._finish(busy=[[7], []], answer=updates.RETRY)
        self.assertEqual((code, started, exe), (0, 1, b"new"))
        self.assertIn("Claude Desktop", texts[0])
        with self.assertLogs(updates.log, "WARNING"):
            code, texts, started, exe = self._finish(busy=[[7]], answer=2)  # Cancel: the old version stays
        self.assertEqual((code, started, exe), (1, 1, b"old"))

    def test_failed_swap_is_told_and_nothing_started(self):
        for failure, text in (
            (PermissionError(5, "Access is denied"), "Прежняя версия на месте"),
            (updates.RestoreError("не вернулась"), "не вернулась"),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.object(updates, "swap", side_effect=failure),
                self.assertLogs(updates.log, "ERROR"),
            ):
                code, texts, started, _ = self._finish()
            self.assertEqual((code, started), (1, 0))
            self.assertIn(text, texts[0])

    def test_wait_for_old_copy_exit(self):
        process_ = subprocess.run([sys.executable, "-c", "pass"], check=True)
        self.assertEqual(process_.returncode, 0)
        self.assertTrue(updates.wait_for_exit(999_999, 0.5))  # no such process: it has already exited


if __name__ == "__main__":
    unittest.main()
