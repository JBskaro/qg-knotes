# SPDX-License-Identifier: GPL-3.0-only
"""Rebuildable in-memory index of the open archive. The window serializes access in background jobs."""

import bisect
import dataclasses
import pathlib
import re
import zlib

from knotes import catalog, minutes, notebook, notes, transcript


@dataclasses.dataclass(frozen=True)
class Hit:
    kind: str
    key: str
    title: str
    excerpt: str
    second: int | None = None


def _match(kind: str, key: str, title: str, original: str, at: int) -> Hit:
    start = original.rfind("\n", 0, at) + 1
    end = original.find("\n", at)
    line = original[start : end if end >= 0 else None]
    position = at - start
    excerpt = line[max(0, position - 45) : position + 105].strip()
    stamp = re.search(r"\[" + transcript.TIME + r"\]", line)
    return Hit(kind, key, title, excerpt, transcript.seconds_list(stamp) if stamp else None)


@dataclasses.dataclass(frozen=True, slots=True)
class TextBlocks:
    starts: list[int]
    blocks: list[bytes]

    @classmethod
    def pack(cls, text: str) -> "TextBlocks":
        starts, blocks = [], []
        start = 0
        while start < len(text):
            newline = text.find("\n", start + 8192)
            end = len(text) if newline < 0 else newline + 1
            starts.append(start)
            blocks.append(zlib.compress(text[start:end].encode("utf-8"), level=1))
            start = end
        return cls(starts, blocks)

    def at(self, offset: int) -> tuple[str, int]:
        index = bisect.bisect_right(self.starts, offset) - 1
        return zlib.decompress(self.blocks[index]).decode("utf-8"), offset - self.starts[index]


@dataclasses.dataclass(slots=True)
class Document:
    kind: str
    key: str
    title: str
    owner: str
    original: str | TextBlocks
    body: str

    @classmethod
    def indexed(cls, identity: tuple, text: str) -> "Document":
        body = catalog.for_search(text)
        if text == body:
            return cls(*identity, body, body)
        original = TextBlocks.pack(text) if len(text) >= 32768 and len(text) == len(body) else text
        return cls(*identity, original, body)

    def match(self, title: str, offset: int) -> Hit:
        if isinstance(self.original, TextBlocks):
            text, offset = self.original.at(offset)
        else:
            text = self.original
        return _match(self.kind, self.key, self.title or title, text, offset)


class ArchiveIndex:
    def __init__(self):
        self.stamps: dict[pathlib.Path, tuple[int, int]] = {}
        self.files: dict[pathlib.Path, list[Document]] = {}

    def _refresh(self, path: pathlib.Path, kind: str, key: str) -> None:
        try:
            stat = path.stat()
            stamp = (stat.st_size, stat.st_mtime_ns)
            if self.stamps.get(path) == stamp:
                return
            if kind == "notebook":
                entries = [
                    (("notebook", n.id, n.title, n.project), n.title + "\n" + n.text)
                    for n in notebook.list_(path.parent.parent)
                ]
            else:
                text = path.read_text(encoding="utf-8")
                if kind == "notes":
                    text = text.replace(notes.PRIVATE_PREFIX, notes.PRIVATE)
                entries = [((kind, key, "", ""), text)]
        except (OSError, UnicodeError):
            self.files.pop(path, None)
            self.stamps.pop(path, None)
            return
        self.files[path] = [Document.indexed(identity, text) for identity, text in entries]
        self.stamps[path] = stamp

    def sync(self, root: pathlib.Path, meetings: list[catalog.Meeting]) -> None:
        paths = {root / notebook.FILE: ("notebook", "")}
        for meeting in meetings:
            if meeting.status != catalog.FILE:
                for kind, name in (
                    ("notes", notes.FILE),
                    ("minutes", minutes.FILE),
                    ("transcript", transcript.TRANSCRIPT),
                ):
                    paths[meeting.folder / name] = (kind, meeting.folder.name)
        for old in self.stamps.keys() - paths.keys():
            del self.stamps[old], self.files[old]
        for path, (kind, key) in paths.items():
            self._refresh(path, kind, key)

    def search(
        self, meetings: list[catalog.Meeting], query: str, project: str | None, include_notes: bool
    ) -> list[Hit]:
        query = catalog.for_search(query.strip())
        titles = {m.folder.name: m.display_title for m in meetings}
        hits = [
            Hit("meeting", m.folder.name, m.display_title, "")
            for m in meetings
            if query in catalog.for_search(" ".join((m.folder.name, m.display_title, m.project)))
        ]
        for documents in self.files.values():
            for doc in documents:
                if doc.kind == "notebook":
                    if not include_notes or project not in (None, doc.owner):
                        continue
                elif doc.key not in titles:
                    continue
                offset = doc.body.find(query)
                if offset >= 0:
                    hits.append(doc.match(titles.get(doc.key, ""), offset))
        return hits
