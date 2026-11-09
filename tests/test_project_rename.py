# SPDX-License-Identifier: GPL-3.0-only
"""Renaming and deleting a project: meetings, notes and own tasks follow it together; a failure halfway leaves
everything as it was, or says which files are not."""

import errno
import pathlib
import tempfile
import unittest
from unittest import mock

from knotes import catalog, project_rename, projects, tasks, transcript

FULL = OSError(errno.ENOSPC, "No space left on device", "встреча.json")


class TestProjectRename(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.recordings = pathlib.Path(temporary.name)
        self.folders = []
        for name in ("2026-10-27_10-00", "2026-10-27_11-00", "2026-10-27_12-00"):
            folder = self.recordings / name
            folder.mkdir()
            (folder / "микрофон.flac").write_bytes(b"x")
            catalog.set_(folder, project="Склад", title=f"Встреча {name}")
            self.folders.append(folder)
        catalog.set_(self.folders[2], project="Остатки")
        projects.set_(self.recordings, "Склад", projects.ProjectNotes("Склад на Ленина"))
        tasks.add(self.recordings, "Сверить остатки", "Склад")
        tasks.add(self.recordings, "Другое", "Остатки")

    def _meetings(self) -> list[catalog.Meeting]:
        return catalog.list_(self.recordings)

    def _files(self) -> dict[pathlib.Path, bytes]:
        return {p: p.read_bytes() for p in sorted(self.recordings.rglob("*")) if p.is_file()}

    def _task_projects(self) -> list[str]:
        return sorted(t.project for t in tasks.tasks(self.recordings, None))

    def test_meetings_notes_and_tasks_follow_the_new_name(self):
        project_rename.rename(self.recordings, self._meetings(), "Склад", "Север")
        self.assertEqual([catalog.properties(f)["проект"] for f in self.folders], ["Север", "Север", "Остатки"])
        self.assertEqual(projects.notes(self.recordings, "Север").text, "Склад на Ленина")
        self.assertEqual(self._task_projects(), ["Остатки", "Север"])
        self.assertEqual(catalog.properties(self.folders[0])["название"], "Встреча 2026-10-27_10-00")

    def test_delete_leaves_meetings_and_tasks_without_a_project(self):
        project_rename.rename(self.recordings, self._meetings(), "Склад", "")
        self.assertEqual([catalog.properties(f).get("проект", "") for f in self.folders], ["", "", "Остатки"])
        self.assertEqual(projects.notes(self.recordings, "Склад").text, "")
        self.assertEqual(self._task_projects(), ["", "Остатки"])

    def test_failure_halfway_puts_everything_back(self):
        """The disk fills up while the tasks are written: the meetings already moved and the notes go back to the
        old name, byte for byte, and the user is told nothing changed."""
        before = self._files()
        with (
            mock.patch.object(project_rename.tasks, "rename_project", side_effect=FULL),
            self.assertLogs(project_rename.log, "WARNING"),
            self.assertRaises(project_rename.NotRenamed) as caught,
        ):
            project_rename.rename(self.recordings, self._meetings(), "Склад", "Север")
        self.assertEqual(self._files(), before)
        self.assertEqual(caught.exception.left, [])
        self.assertIn("закончилось место", caught.exception.text())
        self.assertIn("Ничего не изменилось", caught.exception.text())

    def test_failure_on_the_second_meeting(self):
        before, written = self._files(), []
        original = transcript.write_json

        def write_json(path: pathlib.Path, data: object, indent: int | None = 1) -> None:
            if written:
                raise FULL
            written.append(path)
            original(path, data, indent)

        with (
            mock.patch.object(catalog.transcript, "write_json", side_effect=write_json),
            self.assertLogs(project_rename.log, "WARNING"),
            self.assertRaises(project_rename.NotRenamed),
        ):
            project_rename.rename(self.recordings, self._meetings(), "Склад", "Север")
        self.assertEqual(len(written), 1)
        self.assertEqual(self._files(), before)

    def test_file_that_cannot_be_put_back_is_named(self):
        """The disk takes nothing more once the rename failed: the user learns which files keep the new name."""
        board, broken, original = self.recordings / tasks.FILE, [], transcript.replace

        def full(*_arguments: object) -> None:
            broken.append(True)
            raise FULL

        def replace(source: pathlib.Path, target: pathlib.Path) -> None:
            if broken:
                raise PermissionError(13, "denied", str(target))
            original(source, target)

        with (
            mock.patch.object(project_rename.tasks, "rename_project", side_effect=full),
            mock.patch.object(transcript, "replace", side_effect=replace),
            self.assertLogs(project_rename.log, "WARNING"),
            self.assertRaises(project_rename.NotRenamed) as caught,
        ):
            project_rename.rename(self.recordings, self._meetings(), "Склад", "Север")
        self.assertEqual(
            set(caught.exception.left),
            {
                self.folders[0] / catalog.PROPERTIES,
                self.folders[1] / catalog.PROPERTIES,
                self.recordings / projects.FILE,
            },
        )
        self.assertNotIn(board, caught.exception.left)  # not written before the failure: nothing to put back
        self.assertIn(str(pathlib.Path("2026-10-27_10-00", catalog.PROPERTIES)), caught.exception.text())
        self.assertEqual(list(self.recordings.rglob("*.tmp")), [])

    def test_change_of_another_program_meanwhile_stays(self):
        """While the board file is held and its write is retried, the MCP server adds a task and edits the project
        notes: its task stays, its notes are not written over with the old ones, and the user learns which file
        keeps the new name."""
        notes_file, board = self.recordings / projects.FILE, self.recordings / tasks.FILE

        def held(*_arguments: object) -> None:
            tasks.add(self.recordings, "Задача из MCP", "Остатки")
            transcript.write_json(notes_file, {"Север": {"заметки": "Дописано из MCP"}})
            raise FULL

        with (
            mock.patch.object(project_rename.tasks, "rename_project", side_effect=held),
            self.assertLogs(project_rename.log, "WARNING"),
            self.assertRaises(project_rename.NotRenamed) as caught,
        ):
            project_rename.rename(self.recordings, self._meetings(), "Склад", "Север")
        self.assertIn("Задача из MCP", board.read_text(encoding="utf-8"))
        self.assertIn("Дописано из MCP", notes_file.read_text(encoding="utf-8"))
        self.assertEqual(caught.exception.left, [notes_file])
        self.assertEqual([catalog.properties(f)["проект"] for f in self.folders], ["Склад", "Склад", "Остатки"])

    def test_file_not_read_back_is_not_put_back_blindly(self):
        """A meeting file that can't be read right after the rename wrote it is named, not written over; the notes
        file the rename did not change (no notes of the project) is left as it is."""
        projects.set_(self.recordings, "Склад", projects.ProjectNotes())
        notes_before, reads, original = (self.recordings / projects.FILE).read_bytes(), [], project_rename._content

        def content(path: pathlib.Path) -> bytes | None:
            reads.append(path)
            if len(reads) == 5:  # the four files read first, then the first meeting written
                raise PermissionError(13, "denied", str(path))
            return original(path)

        with (
            mock.patch.object(project_rename, "_content", side_effect=content),
            mock.patch.object(project_rename.tasks, "rename_project", side_effect=FULL),
            self.assertLogs(project_rename.log, "WARNING"),
            self.assertRaises(project_rename.NotRenamed) as caught,
        ):
            project_rename.rename(self.recordings, self._meetings(), "Склад", "Север")
        self.assertEqual(caught.exception.left, [reads[4]])
        self.assertEqual(catalog.properties(reads[4].parent)["проект"], "Север")
        self.assertEqual((self.recordings / projects.FILE).read_bytes(), notes_before)

    def test_file_the_rename_created_is_removed_again(self):
        """The first project notes: the file the failed rename created goes away."""
        notes_file = self.recordings / projects.FILE
        notes_file.unlink()
        with (
            mock.patch.object(
                project_rename.projects, "rename", side_effect=lambda *_: transcript.write_json(notes_file, {})
            ),
            mock.patch.object(project_rename.tasks, "rename_project", side_effect=FULL),
            self.assertLogs(project_rename.log, "WARNING"),
            self.assertRaises(project_rename.NotRenamed),
        ):
            project_rename.rename(self.recordings, self._meetings(), "Склад", "Север")
        self.assertFalse((self.recordings / projects.FILE).exists())

    def test_unreadable_file_stops_before_anything_is_written(self):
        before = self._files()
        with (
            mock.patch.object(pathlib.Path, "read_bytes", side_effect=PermissionError(13, "denied")),
            self.assertLogs(project_rename.log, "WARNING"),
            self.assertRaises(project_rename.NotRenamed),
        ):
            project_rename.rename(self.recordings, self._meetings(), "Склад", "Север")
        self.assertEqual(self._files(), before)

    def test_without_recordings_folder_only_meetings(self):
        """The window before its first list reading has no recordings folder: notes and tasks stay."""
        project_rename.rename(None, self._meetings(), "Склад", "Север")
        self.assertEqual(catalog.properties(self.folders[0])["проект"], "Север")
        self.assertEqual(self._task_projects(), ["Остатки", "Склад"])


if __name__ == "__main__":
    unittest.main()
