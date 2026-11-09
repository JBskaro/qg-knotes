# SPDX-License-Identifier: GPL-3.0-only
import contextlib
import io
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
import winreg
from unittest import mock

import build
from knotes import updates


class FindIsccTest(unittest.TestCase):
    def setUp(self):
        self.folder = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def iscc(self, folder: pathlib.Path) -> pathlib.Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "ISCC.exe"
        path.write_bytes(b"")
        return path

    def test_path_first(self):
        on_path = self.iscc(self.folder / "path")
        self.iscc(self.folder / "installed")
        with (
            mock.patch.object(build.shutil, "which", return_value=str(on_path)),
            mock.patch.object(build, "inno_folders", return_value=[self.folder / "installed"]),
        ):
            self.assertEqual(build.find_iscc(), on_path)

    def test_first_installed_folder_with_iscc(self):
        found = self.iscc(self.folder / "6")
        with (
            mock.patch.object(build.shutil, "which", return_value=None),
            mock.patch.object(build, "inno_folders", return_value=[self.folder / "7", self.folder / "6"]),
        ):
            self.assertEqual(build.find_iscc(), found)

    def test_not_installed(self):
        with (
            mock.patch.object(build.shutil, "which", return_value=None),
            mock.patch.object(build, "inno_folders", return_value=[self.folder / "7"]),
        ):
            self.assertIsNone(build.find_iscc())

    def test_newest_version_and_default_folders(self):
        registered = {"Inno Setup 7_is1": [pathlib.Path(r"D:\Inno7")], "Inno Setup 6_is1": []}
        with (
            mock.patch.object(build, "registered_folders", side_effect=registered.__getitem__),
            mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Local", "ProgramFiles": r"C:\PF"}, clear=True),
        ):
            folders = build.inno_folders()
        self.assertEqual(
            folders[:3],
            [
                pathlib.Path(r"D:\Inno7"),
                pathlib.Path(r"C:\Local\Programs\Inno Setup 7"),
                pathlib.Path(r"C:\PF\Inno Setup 7"),
            ],
        )
        self.assertIn(pathlib.Path(r"C:\Local\Programs\Inno Setup 6"), folders)

    def test_registered_folder_from_uninstall_entry(self):
        def open_key(root, key, _reserved, _access):
            if root != winreg.HKEY_CURRENT_USER:
                raise FileNotFoundError(key)
            self.assertTrue(key.endswith(r"\Uninstall\Inno Setup 7_is1"))
            return contextlib.nullcontext("entry")

        with (
            mock.patch.object(build.winreg, "OpenKey", side_effect=open_key),
            mock.patch.object(build.winreg, "QueryValueEx", return_value=(r"E:\Tools\Inno Setup 7", 1)),
        ):
            self.assertEqual(build.registered_folders("Inno Setup 7_is1"), [pathlib.Path(r"E:\Tools\Inno Setup 7")])


class BuildInstallerTest(unittest.TestCase):
    source, icon = pathlib.Path(r"C:\build\QG Knotes"), pathlib.Path(r"C:\build\app.ico")

    def build(self, iscc, returncode=0):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], returncode))
        output = io.StringIO()
        with (
            mock.patch.object(build, "find_iscc", return_value=iscc),
            mock.patch.object(build.subprocess, "run", run),
            contextlib.redirect_stdout(output),
        ):
            setup = build.build_installer(self.source, self.icon)
        return setup, run, output.getvalue()

    def test_without_inno_setup_only_zip(self):
        setup, run, output = self.build(None)
        self.assertIsNone(setup)
        run.assert_not_called()
        self.assertIn("Inno Setup", output)
        self.assertIn("https://jrsoftware.org", output)

    def test_compiles_script_with_version_and_folder(self):
        setup, run, _ = self.build(pathlib.Path(r"C:\Inno\ISCC.exe"))
        command = run.call_args.args[0]
        self.assertEqual(command[0], r"C:\Inno\ISCC.exe")
        self.assertIn(f"/DAppVersion={build.VERSION}", command)
        self.assertIn(f"/DSourceDir={self.source}", command)
        self.assertIn(f"/DIconFile={self.icon}", command)
        self.assertIn(f"/O{build.RESULT}", command)
        self.assertEqual(command[-1], str(build.INNO_SCRIPT))
        self.assertEqual(setup, build.RESULT / f"QG-Knotes-{build.VERSION}-setup.exe")

    def test_compile_error_stops_build(self):
        with self.assertRaises(SystemExit) as stop:
            self.build(pathlib.Path(r"C:\Inno\ISCC.exe"), returncode=2)
        self.assertIn("2", str(stop.exception.code))


class InnoScriptTest(unittest.TestCase):
    script = build.INNO_SCRIPT.read_text(encoding="utf-8")

    def test_ascii_only(self):
        # Plain ASCII compiles the same with any code page and Inno Setup version
        self.assertTrue(self.script.isascii())

    def test_setup_name_matches_build_and_updates(self):
        name = build.setup_path().name
        self.assertIn(
            f"OutputBaseFilename={name.replace(build.VERSION, '{#AppVersion}').removesuffix('.exe')}\n", self.script
        )
        self.assertTrue(name.startswith("QG-Knotes-") and name.endswith("-setup.exe"))  # updates.asset_for looks for it

    def test_per_user_install_keeps_app_id(self):
        self.assertIn("PrivilegesRequired=lowest\n", self.script)
        self.assertIn("DefaultDirName={autopf}\\{#AppName}\n", self.script)
        self.assertIn("AppId={{2E6B7F9B-78D3-4A19-B42A-FC6B2EEFC593}\n", self.script)

    def test_update_starts_the_program_again(self):
        # The program runs the installer quietly and exits: the installer has to start the new version itself
        self.assertIn("/LAUNCH=1", updates.SETUP_ARGS)
        self.assertIn("{param:launch|0}", self.script)


if __name__ == "__main__":
    unittest.main()
