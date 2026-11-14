# SPDX-License-Identifier: GPL-3.0-only
"""Rich note editing and compatibility with the original Markdown notes."""

import re
from collections.abc import Iterator

from PySide6.QtCore import QByteArray, Qt, QUrl
from PySide6.QtGui import (
    QFont,
    QKeyEvent,
    QSyntaxHighlighter,
    QTextBlock,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextListFormat,
    QTextTableFormat,
    QTextLength,
)
from PySide6.QtWidgets import QTextEdit

MARKDOWN = QTextDocument.MarkdownFeature.MarkdownDialectGitHub | QTextDocument.MarkdownFeature.MarkdownNoHTML
HEADING_CSS = "h1, h2, h3, h4, h5, h6 { font-weight: normal; font-size: medium; }"
BOLD, ITALIC, STRIKE = (
    QTextFormat.Property.FontWeight,
    QTextFormat.Property.FontItalic,
    QTextFormat.Property.FontStrikeOut,
)
HEADING = re.compile(r"^(#{1,6}) (.*)$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
CHECK = re.compile(r"^- \[([ xX])\] (.*)$")
DISC, NUMBER = QTextListFormat.Style.ListDisc, QTextListFormat.Style.ListDecimal
Marker = QTextBlockFormat.MarkerType


def link_url(address: str) -> QUrl | None:
    url = QUrl(address)
    if not url.isValid():
        return None
    scheme = url.scheme().lower()
    if (scheme in ("http", "https") and url.host()) or (scheme == "mailto" and url.path()):
        return url
    return None


class NoteDocument(QTextDocument):
    def loadResource(self, _type: int, _name: QUrl) -> QByteArray:
        return QByteArray()


class HeadingStyle(QSyntaxHighlighter):
    def highlightBlock(self, _text: str) -> None:
        block = self.currentBlock()
        if level := block.blockFormat().headingLevel():
            fmt = QTextCharFormat()
            fmt.setFontWeight(QFont.Weight.Bold)
            fmt.setProperty(QTextFormat.Property.FontSizeAdjustment, max(1, 5 - level))
            self.setFormat(0, block.length() - 1, fmt)


class NoteEditor(QTextEdit):
    def __init__(self):
        super().__init__()
        self.setDocument(NoteDocument(self))
        self.setAcceptRichText(False)
        self.setTabChangesFocus(False)
        self._headings = HeadingStyle(self.document())

    def load_text(self, text: str, rich_text: str = "") -> None:
        self.setUndoRedoEnabled(False)
        if rich_text:
            self.document().setDefaultStyleSheet(HEADING_CSS)
            self.setHtml(rich_text)
        else:
            self._load_legacy(text)
        cursor = QTextCursor(self.document())
        self.setTextCursor(cursor)
        self.setUndoRedoEnabled(True)
        self.document().setModified(False)

    def _load_legacy(self, text: str) -> None:
        self.setPlainText("")
        cursor = QTextCursor(self.document())
        fence = ""
        for number, line in enumerate(text.split("\n")):
            if number:
                cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
            if match := FENCE.match(line):
                delimiter, tail = match.groups()
                if not fence:
                    fence = delimiter
                elif delimiter.startswith(fence) and not tail.strip():
                    fence = ""
            if heading := HEADING.match(line) if not fence else None:
                self._set_block(cursor, heading=len(heading[1]))
                line = heading[2]
            elif item := CHECK.match(line) if not fence else None:
                marker = Marker.Checked if item[1].lower() == "x" else Marker.Unchecked
                self._assign_list(cursor, DISC, marker)
                line = item[2]
            cursor.insertText(line, QTextCharFormat())

    def source(self) -> str:
        copy = self.document().clone()
        cursor = QTextCursor(copy)
        while not cursor.atEnd():
            if table := cursor.currentTable():
                for column in range(table.columns()):
                    cell = table.cellAt(0, column)
                    if cell.firstPosition() == cell.lastPosition():
                        cell.firstCursorPosition().insertText(" ")
                cursor.setPosition(table.lastPosition() + 1)
            elif not cursor.movePosition(QTextCursor.MoveOperation.NextBlock):
                break
        return copy.toMarkdown(MARKDOWN).rstrip("\n")

    @staticmethod
    def _set_block(cursor: QTextCursor, heading: int = 0, marker: Marker = Marker.NoMarker) -> None:
        fmt = cursor.blockFormat()
        fmt.setHeadingLevel(heading)
        fmt.setMarker(marker)
        cursor.setBlockFormat(fmt)

    def _blocks(self) -> Iterator[QTextBlock]:
        cursor = self.textCursor()
        block = self.document().findBlock(cursor.selectionStart())
        limit = max(cursor.selectionStart(), cursor.selectionEnd() - 1)
        while block.isValid() and block.position() <= limit:
            yield block
            block = block.next()

    def set_heading(self, level: int) -> None:
        cursor = self.textCursor()
        cursor.beginEditBlock()
        for block in self._blocks():
            line = QTextCursor(block)
            if level and (items := line.currentList()) is not None:
                items.remove(block)
            self._set_block(line, heading=level)
            self._headings.rehighlightBlock(block)
        cursor.endEditBlock()

    def toggle_style(self, prop: QTextFormat.Property) -> None:
        fmt = QTextCharFormat()
        current = self.currentCharFormat()
        if prop == BOLD:
            weight = QFont.Weight.Normal if current.fontWeight() >= QFont.Weight.Bold else QFont.Weight.Bold
            fmt.setFontWeight(weight)
        else:
            fmt.setProperty(prop, not bool(current.property(prop)))
        self.mergeCurrentCharFormat(fmt)

    def set_link(self, address: QUrl) -> None:
        cursor = self.textCursor()
        text = address.toString(QUrl.ComponentFormattingOption.FullyEncoded)
        fmt = QTextCharFormat()
        fmt.setAnchor(True)
        fmt.setAnchorHref(text)
        fmt.setFontUnderline(True)
        fmt.setForeground(self.palette().link())
        cursor.beginEditBlock()
        if cursor.hasSelection():
            cursor.mergeCharFormat(fmt)
        else:
            cursor.insertText(text, fmt)
            cursor.setCharFormat(QTextCharFormat())
            self.setTextCursor(cursor)
        cursor.endEditBlock()

    @staticmethod
    def _assign_list(cursor: QTextCursor, kind: QTextListFormat.Style, marker: Marker) -> None:
        fmt = QTextListFormat()
        fmt.setStyle(kind)
        previous = cursor.block().previous()
        items = previous.textList() if previous.isValid() else None
        if (
            items is not None
            and items.format().style() == kind
            and (previous.blockFormat().marker() == Marker.NoMarker) == (marker == Marker.NoMarker)
        ):
            items.add(cursor.block())
        else:
            cursor.createList(fmt)
        NoteEditor._set_block(cursor, marker=marker)

    def set_list(self, kind: QTextListFormat.Style | None, checklist: bool = False) -> None:
        cursor = self.textCursor()
        cursor.beginEditBlock()
        for block in self._blocks():
            line = QTextCursor(block)
            if items := block.textList():
                items.remove(block)
            if kind is not None:
                marker = Marker.Unchecked if checklist else Marker.NoMarker
                self._assign_list(line, kind, marker)
            else:
                self._set_block(line, heading=line.blockFormat().headingLevel())
        cursor.endEditBlock()

    def toggle_check(self) -> None:
        cursor = self.textCursor()
        mark = cursor.blockFormat().marker()
        if mark == Marker.NoMarker:
            self.set_list(DISC, checklist=True)
        else:
            self._set_block(cursor, marker=Marker.Unchecked if mark == Marker.Checked else Marker.Checked)

    def clear_formatting(self) -> None:
        cursor = self.textCursor()
        cursor.beginEditBlock()
        self.set_list(None)
        self.set_heading(0)
        self.setCurrentCharFormat(QTextCharFormat())
        cursor.endEditBlock()

    def insert_table(self, rows: int, columns: int) -> None:
        cursor = self.textCursor()
        if cursor.currentTable() is not None:
            return
        fmt = QTextTableFormat()
        fmt.setHeaderRowCount(1)
        fmt.setBorderCollapse(True)
        fmt.setBorder(1)
        fmt.setBorderBrush(self.palette().mid())
        fmt.setCellPadding(5)
        fmt.setWidth(QTextLength(QTextLength.Type.PercentageLength, 92))
        cursor.beginEditBlock()
        table = cursor.insertTable(rows, columns, fmt)
        self.setTextCursor(table.cellAt(0, 0).firstCursorPosition())
        cursor.endEditBlock()

    def edit_table(self, operation: str) -> None:
        cursor = self.textCursor()
        table = cursor.currentTable()
        if table is None:
            return
        cell = table.cellAt(cursor)
        row, column = cell.row(), cell.column()
        match operation:
            case "row+":
                table.insertRows(row + 1, 1)
            case "col+":
                table.insertColumns(column + 1, 1)
            case "row-" if table.rows() > 1:
                table.removeRows(row, 1)
            case "col-" if table.columns() > 1:
                table.removeColumns(column, 1)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        cursor = self.textCursor()
        if event.key() not in (Qt.Key.Key_Return, Qt.Key.Key_Enter) or event.modifiers():
            super().keyPressEvent(event)
            return
        heading = cursor.blockFormat().headingLevel()
        marker = cursor.blockFormat().marker()
        before = cursor.block()
        cursor.beginEditBlock()
        if cursor.charFormat().isAnchor() and cursor.atBlockEnd() and not cursor.hasSelection():
            cursor.insertBlock(cursor.blockFormat(), QTextCharFormat())
            self.setTextCursor(cursor)
        else:
            super().keyPressEvent(event)
        after = self.textCursor()
        if after.block() != before and heading:
            self._set_block(after)
            self.setCurrentCharFormat(QTextCharFormat())
        elif after.block() != before and after.currentList() is not None and marker != Marker.NoMarker:
            self._set_block(after, marker=Marker.Unchecked)
        cursor.endEditBlock()
