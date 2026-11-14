# SPDX-License-Identifier: GPL-3.0-only
"""Meeting notepad: the analyst's notes with recording minutes and the agenda on top, like in Granola and Fellow.

During recording the notepad is a toast over the windows (Ctrl+Alt+N or the pencil on the recording bar) that is hidden
during screen sharing; after the meeting it is the "Notes" tab of the meeting. A line started during recording gets the
recording minute: it shows on the left in gray, a click on it opens that spot of the transcript. With the transcript
during the meeting on (live.py), the "Now" section under the notes shows the latest phrases and takes questions.
"""

import contextlib
import html
import logging
import pathlib
from collections.abc import Callable

from PySide6.QtCore import QMimeData, QPoint, QRect, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QResizeEvent,
    QShowEvent,
    QSyntaxHighlighter,
    QTextBlock,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSizeGrip,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from knotes import notes, tasks, catalog, transcript, system, failures, live
from knotes import settings
from knotes.ui import style
from knotes.ui.background import run_in_background
from knotes.ui.minutes_view import SHADES
from knotes.language import tr

MINUTES_WIDTH = 46
KIND_COLOR = {"Решение": "зеленый", "Задача": "синий", "Вопрос": "янтарный"}  # like sections in the minutes; also tr()
DENSITY = 246  # the notepad is read and typed in: almost opaque, unlike the recording bar
TOAST_STYLE = """
QPlainTextEdit, QListWidget { background: transparent; border: none; color: #F1EFE8; font-size: 14px; }
QListWidget::item { padding: 2px 0; }
QLineEdit { color: #F1EFE8; background: transparent; border: 1px solid transparent; border-radius: 6px;
            padding: 3px 8px; font-size: 13px; }
QLineEdit:hover, QLineEdit:focus { background: rgba(255,255,255,0.06); border-color: rgba(255,255,255,0.12); }
QComboBox { color: #F1EFE8; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12);
            border-radius: 6px; padding: 2px 8px; font-size: 13px; }
QToolButton { color: #D3D1C7; background: transparent; border: none; padding: 2px 6px; font-size: 13px; }
QToolButton:hover { color: #FFFFFF; background: rgba(255,255,255,0.10); border-radius: 6px; }
QPushButton#вид { padding: 2px 10px; font-size: 12px; }
QListWidget#сейчас { font-size: 13px; }
QLabel#сейчас_ход { color: #888780; font-size: 12px; }
QLabel#ошибка_текст { color: #F09595; font-size: 12px; }
"""
LINK_COLOR = "#9FC5F0"  # recording minutes in the answer: readable on the dark notepad
SPARE_FOLDER = "Несохраненные заметки"  # next to the settings: notes that did not reach the meeting folder
log = logging.getLogger(__name__)


def _rgb(shade: str) -> QColor:
    return QColor(*(int(ch) for ch in SHADES[shade].split(",")))


class Highlighter(QSyntaxHighlighter):
    """Headings "# " larger, the KIND_COLOR words followed by " -" (also in the interface language) in the color of
    their section, questions in amber, checked boxes "- [x]" gray and struck out."""

    def highlightBlock(self, text: str) -> None:
        if notes.PRIVATE in text:
            muted = QTextCharFormat()
            muted.setForeground(_rgb("янтарный"))
            self.setFormat(0, len(text.encode("utf-16-le")) // 2, muted)
            return
        if text.startswith("#"):
            kind = QTextCharFormat()
            kind.setFontWeight(QFont.Weight.DemiBold)
            kind.setFontPointSize(self.document().defaultFont().pointSizeF() * 1.15)
            self.setFormat(0, len(text), kind)
            grid = QTextCharFormat(kind)
            grid.setForeground(QColor("#888780"))  # the heading mark is quieter than the heading itself
            self.setFormat(0, len(text) - len(text.lstrip("# ")), grid)
            return
        if text.lstrip().startswith("- [x]"):
            kind = QTextCharFormat()
            kind.setForeground(QColor("#888780"))
            kind.setFontStrikeOut(True)
            self.setFormat(0, len(text), kind)
            return
        colors = KIND_COLOR | {tr(s): c_ for s, c_ in KIND_COLOR.items()}
        word = next((s for s in colors if text.startswith(f"{s} -")), "")
        if word:
            kind = QTextCharFormat()
            kind.setForeground(_rgb(colors[word]))
            kind.setFontWeight(QFont.Weight.DemiBold)
            self.setFormat(0, len(word), kind)
        elif notes.question(text):
            kind = QTextCharFormat()
            kind.setForeground(_rgb("янтарный"))
            self.setFormat(0, len(text), kind)


class MinutesField(QWidget):
    """Left of the lines: the recording minute when a line was started; a click opens that spot in the transcript."""

    def __init__(self, editor: "NotesEditor"):
        super().__init__(editor)
        self.editor = editor
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:
        return QSize(MINUTES_WIDTH, 0)

    def _lines(self) -> list[tuple[QTextBlock, int, int]]:
        """Visible lines with a minute: the line, its top and height in the field."""
        result, block = [], self.editor.firstVisibleBlock()
        shift = self.editor.contentOffset()
        while block.isValid():
            place = self.editor.blockBoundingGeometry(block).translated(shift)
            if place.top() > self.height():
                break
            if block.userState() > 0:
                result.append((block, int(place.top()), int(place.height())))
            block = block.next()
        return result

    def paintEvent(self, _event_of: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setPen(QColor(self.editor.minutes_color))
        font = self.font()
        font.setPointSizeF(font.pointSizeF() * 0.85)
        painter.setFont(font)
        row_height = self.editor.fontMetrics().height()
        for block, top, _height in self._lines():
            painter.drawText(
                QRect(0, top, MINUTES_WIDTH - 8, row_height),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                transcript.time(block.userState() - 1),
            )
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        for block, top, height in self._lines():
            if top <= event.position().y() <= top + height:
                self.editor.to_place.emit(float(block.userState() - 1))


class NotesEditor(QPlainTextEdit):
    """Notes text. During recording (clock gives how many seconds the recording has run) a new line gets the minute
    when its first character is typed. Ctrl+Enter sets or clears the "- [ ]" checkbox of the line."""

    to_place = Signal(float)

    def __init__(self, clock: Callable[[], float] | None = None, minutes_color: str = "#888780"):
        super().__init__()
        self.clock, self.minutes_color = clock, minutes_color
        self.field = MinutesField(self)
        self.setViewportMargins(MINUTES_WIDTH, 0, 0, 0)
        self.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self._highlight_state = Highlighter(self.document())
        self.updateRequest.connect(lambda *_: self.field.update())
        self.textChanged.connect(self._mark_minute)
        self.setPlaceholderText(
            tr(
                'Write what matters: each line gets the recording minute. "Decision -", "Task -", "Question -" '
                "at the start tell the model the section of the minutes"
            )
            if clock
            else tr("Your notes on the meeting")
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        place = self.contentsRect()
        self.field.setGeometry(QRect(place.left(), place.top(), MINUTES_WIDTH, place.height()))

    def _mark_minute(self) -> None:
        block = self.textCursor().block()
        if self.clock is not None and block.userState() <= 0 and block.text().strip():
            block.setUserState(int(self.clock()) + 1)
            self.field.update()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        stdin = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if stdin and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.checkbox()
            return
        cursor = self.textCursor()
        at_start = stdin and cursor.positionInBlock() == 0 and bool(cursor.block().text())
        minute = cursor.block().userState()
        if stdin and notes.PRIVATE in cursor.block().text():
            cursor.beginEditBlock()
            try:
                super().keyPressEvent(event)
                self._keep_private_split()
            finally:
                cursor.endEditBlock()
        else:
            super().keyPressEvent(event)
        if at_start:  # the line moved down: its minute goes with it, the empty line above gets none
            lower = self.textCursor().block()
            lower.setUserState(minute)
            lower.previous().setUserState(-1)
        elif stdin and not self.textCursor().block().text().strip():
            self.textCursor().block().setUserState(-1)  # the new line gets its own minute, not the neighbor's
        self.field.update()

    def insertFromMimeData(self, source: QMimeData) -> None:
        cursor = self.textCursor()
        first = self.document().findBlock(cursor.selectionStart())
        if not source.hasText() or not self._includes_private(first, cursor.selectionEnd()):
            super().insertFromMimeData(source)
            return
        cursor.beginEditBlock()
        try:
            cursor.insertText(source.text())
            self.setTextCursor(cursor)
            for number in range(first.blockNumber(), cursor.blockNumber() + 1):
                self._keep_private(self.document().findBlockByNumber(number))
        finally:
            cursor.endEditBlock()

    @staticmethod
    def _includes_private(block: QTextBlock, end: int) -> bool:
        while block.isValid() and block.position() <= end:
            if notes.PRIVATE in block.text():
                return True
            block = block.next()
        return False

    def _keep_private_split(self) -> None:
        cursor = self.textCursor()
        for block in (cursor.block().previous(), cursor.block()):
            self._keep_private(block)

    @staticmethod
    def _keep_private(block: QTextBlock) -> None:
        if block.isValid() and block.text().strip() and notes.PRIVATE not in block.text():
            QTextCursor(block).insertText(f"{notes.PRIVATE} ")

    def toggle_private(self) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        line = notes.NoteLine.from_body(cursor.selectedText())
        cursor.insertText(f"{notes.PRIVATE} {line.text}" if not line.private else line.text)
        self.setTextCursor(cursor)
        self.setFocus()

    def checkbox(self) -> None:
        """A checkbox for the whole line (paragraph), not for its visible part when it wraps."""
        line = self.textCursor()
        line.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        line.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        current = notes.NoteLine.from_body(line.selectedText())
        text = current.text
        if text.startswith(("- [ ] ", "- [x] ")):
            text = ("- [x] " if text.startswith("- [ ] ") else "- [ ] ") + text[6:]
        else:
            text = "- [ ] " + text
        line.insertText(notes.NoteLine(text, private=current.private).body())

    def insert_kind(self, kind: str) -> None:
        """Puts "<kind> - " at the start of the line; in an empty line typing continues right after it."""
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        line = notes.NoteLine.from_body(cursor.selectedText())
        checkbox = line.text[:6] if line.text.startswith(("- [ ] ", "- [x] ")) else ""
        body = line.text[len(checkbox) :]
        prefixes = (*notes.KINDS, *(tr(word) for word in notes.KINDS))
        text = next((body[len(word) + 3 :] for word in prefixes if body.startswith(f"{word} - ")), body)
        cursor.insertText(notes.NoteLine(f"{checkbox}{kind} - {text}", private=line.private).body())
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        self.setTextCursor(cursor)
        self.setFocus()

    def add_line(self, text: str, second: float) -> None:
        """A line at the end of the notes with the given recording minute: a phrase of the transcript."""
        self.moveCursor(QTextCursor.MoveOperation.End)
        self.insertPlainText(f"\n{text}" if self.textCursor().block().text().strip() else text)
        self.textCursor().block().setUserState(int(second) + 1)
        self.field.update()

    def set_(self, lines: tuple[notes.NoteLine, ...]) -> None:
        self.blockSignals(True)
        self.setPlainText("\n".join(s.body() for s in lines))
        block = self.document().firstBlock()
        for line in lines:
            block.setUserState(line.second + 1 if line.second is not None else -1)
            block = block.next()
        self.blockSignals(False)
        self.field.update()

    def lines(self) -> tuple[notes.NoteLine, ...]:
        result, block = [], self.document().firstBlock()
        while block.isValid():
            result.append(
                notes.NoteLine.from_body(block.text(), block.userState() - 1 if block.userState() > 0 else None)
            )
            block = block.next()
        return tuple(result)


class AgendaPanel(QWidget):
    """Meeting agenda as checkboxes: checked means discussed; an item is added in the bottom line, removed by right
    click."""

    meeting_changed = Signal()

    def __init__(self):
        super().__init__()
        self._header = QToolButton()
        self._header.setObjectName("повестка")
        self._header.clicked.connect(lambda: self._expand(not self.list_.isVisible()))
        self.list_ = QListWidget()
        self.list_.setFrameShape(QListWidget.Shape.NoFrame)
        self.list_.setWordWrap(True)
        self.list_.itemChanged.connect(self._altered)
        self.list_.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_.customContextMenuRequested.connect(self._menu)
        self.new = QLineEdit()
        self.new.setPlaceholderText(tr("+ Agenda item, Enter"))
        self.new.returnPressed.connect(self._add)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        column.addWidget(self._header, 0, Qt.AlignmentFlag.AlignLeft)
        column.addWidget(self.list_)
        column.addWidget(self.new)
        self._expanded = True
        self._expand(True)

    def _expand(self, yes: bool) -> None:
        self._expanded = yes
        self.list_.setVisible(yes)
        self.new.setVisible(yes)
        self._label_items()

    def _label_items(self) -> None:
        total = self.list_.count()
        discussed = sum(self.list_.item(n).checkState() == Qt.CheckState.Checked for n in range(total))
        account = tr("Agenda · {discussed} of {total}", discussed=discussed, total=total) if total else tr("Agenda")
        self._header.setText(f"{account}  {'▾' if self._expanded else '▸'}")
        line = max(self.list_.sizeHintForRow(0) if total else 0, self.fontMetrics().height() + 10)
        self.list_.setFixedHeight(min(total, 6) * line + 4)
        self.list_.setVisible(self._expanded and total > 0)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._label_items()  # row heights are known only after styling

    def _element(self, item: notes.AgendaItem) -> QListWidgetItem:
        element = QListWidgetItem(item.text)
        element.setFlags(element.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEditable)
        element.setCheckState(Qt.CheckState.Checked if item.discussed else Qt.CheckState.Unchecked)
        return element

    def set_(self, items: tuple[notes.AgendaItem, ...] | list[notes.AgendaItem]) -> None:
        self.list_.blockSignals(True)
        self.list_.clear()
        for item in items:
            self.list_.addItem(self._element(item))
        self.list_.blockSignals(False)
        self._label_items()

    def items(self) -> tuple[notes.AgendaItem, ...]:
        elements = (self.list_.item(n) for n in range(self.list_.count()))
        return tuple(
            notes.AgendaItem(e_.text().strip(), e_.checkState() == Qt.CheckState.Checked)
            for e_ in elements
            if e_.text().strip()
        )

    def _altered(self) -> None:
        self._label_items()
        self.meeting_changed.emit()

    def _add(self) -> None:
        if text := self.new.text().strip():
            self.list_.addItem(self._element(notes.AgendaItem(text)))
            self.new.clear()
            self._altered()

    def _menu(self, point: QPoint) -> None:
        element = self.list_.itemAt(point)
        if element is None:
            return
        menu = QMenu(self)
        menu.addAction(
            tr("Remove from agenda"), lambda: (self.list_.takeItem(self.list_.row(element)), self._altered())
        )
        menu.exec(self.list_.viewport().mapToGlobal(point))


class NotesReview(QWidget):
    to_place = Signal(float)
    play_at = Signal(float)
    edit_line = Signal(int)

    def __init__(self, folder: pathlib.Path):
        super().__init__()
        self.folder = folder
        self._entries: list[tuple[float | None, int | None, str]] = []
        self._audio = self._playing = False
        self.search = style.ClearableLine()
        self.search.setPlaceholderText(tr("Find in notes"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._show_matches)
        self.search.returnPressed.connect(self._activate)
        self.list_ = QListWidget()
        self.list_.setWordWrap(True)
        self.list_.itemActivated.connect(self._activate)
        self.list_.currentItemChanged.connect(self._selection)
        self.empty = style.caption("")
        self.edit_button = QPushButton(tr("Edit note"))
        self.edit_button.clicked.connect(self._edit)
        self.play_button = QPushButton(tr("Listen"))
        self.play_button.clicked.connect(self._play)
        actions = QHBoxLayout()
        actions.addWidget(self.play_button)
        actions.addWidget(self.edit_button)
        actions.addStretch()
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(self.search)
        column.addWidget(self.list_, 1)
        column.addWidget(self.empty)
        column.addLayout(actions)
        self.setFocusProxy(self.search)

    def set_lines(self, lines: tuple[notes.NoteLine, ...]) -> None:
        found = catalog.meeting(self.folder)
        self._audio = bool(found and found.has_audio)
        self._entries = [(line.second, n, line.body()) for n, line in enumerate(lines) if line.text.strip()]
        self._entries.extend(
            (mark["время"], None, tr(transcript.MARKER))
            for mark in transcript.labels(self.folder)
            if mark["текст"] == transcript.MARKER
        )
        self._entries.sort(key=lambda entry: entry[0] if entry[0] is not None else float("inf"))
        self._show_matches()

    def _show_matches(self) -> None:
        self.list_.clear()
        query = catalog.for_search(self.search.text().strip())
        for second, number, text in self._entries:
            when = transcript.time(second) if second is not None else ""
            caption = f"{when}   {text}" if when else text
            if query not in catalog.for_search(caption):
                continue
            item = QListWidgetItem(caption)
            item.setData(Qt.ItemDataRole.UserRole, (second, number))
            self.list_.addItem(item)
        self.empty.setText(tr("No matching notes") if query else tr("No notes or marks yet"))
        self.empty.setVisible(not self.list_.count())
        self.list_.setCurrentRow(0)
        self._selection()

    def _selected(self) -> tuple[float | None, int | None]:
        item = self.list_.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else (None, None)

    def _selection(self) -> None:
        second, number = self._selected()
        self.play_button.setEnabled(self._audio and (self._playing or second is not None))
        self.edit_button.setEnabled(number is not None)

    def _activate(self) -> None:
        second, number = self._selected()
        if second is not None and (self.folder / transcript.PHRASES).is_file():
            self.to_place.emit(second)
        elif number is not None:
            self.edit_line.emit(number)
        elif self.play_button.isEnabled():
            self._play()

    def playing(self, active: bool) -> None:
        self._playing = active
        self.play_button.setText(tr("Stop listening") if active else tr("Listen"))
        self._selection()

    def _play(self) -> None:
        second, _ = self._selected()
        if second is not None or self._playing:
            self.play_at.emit(second if second is not None else 0.0)

    def _edit(self) -> None:
        _, number = self._selected()
        if number is not None:
            self.edit_line.emit(number)


class Notepad(QWidget):
    """Agenda, notes and quick marks; saves itself while the user types and on closing."""

    to_place = Signal(float)
    mark = Signal()  # "★" during recording
    play_at = Signal(float)
    saved = Signal()

    def __init__(self, folder: pathlib.Path, clock: Callable[[], float] | None = None, on_toast: bool = False):
        super().__init__()
        self.folder, self._modified = folder, False
        self._loaded = notes.Notes()
        self._spare: pathlib.Path | None = None  # a copy of the notes that did not save
        self.agenda = AgendaPanel()
        self.agenda.meeting_changed.connect(self._later)
        self.editor = NotesEditor(clock, "#888780" if on_toast else style.text_tones()[1])
        self.editor.to_place.connect(self.to_place)
        self.editor.textChanged.connect(self._later)
        self._save_later = QTimer(self, singleShot=True, interval=700)
        self._save_later.timeout.connect(self.save)
        self.failure = QLabel()  # why the notes are not saved; the text stays in the notepad
        self.failure.setObjectName("ошибка_текст")
        self.failure.setWordWrap(True)
        self.failure.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)  # the copy's path
        self.failure.hide()
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        column.addWidget(self.agenda)
        self.review = NotesReview(folder) if clock is None else None
        if self.review is None:
            column.addWidget(self.editor, 1)
        else:
            self._build_views(column)
        column.addWidget(self.failure)
        self.tools = QWidget()
        self.tools.setLayout(self._note_marks(clock is not None))
        column.addWidget(self.tools)
        self.setFocusProxy(self.editor)
        self.load()

    def _build_views(self, column: QVBoxLayout) -> None:
        self.views = QStackedWidget()
        self.views.addWidget(self.editor)
        self.views.addWidget(self.review)
        self.tabs = style.Segments([(0, tr("Edit notes")), (1, tr("By time"))], 0)
        self.tabs.selected.connect(self._show_view)
        self.review.to_place.connect(self.to_place)
        self.review.play_at.connect(self.play_at)
        self.review.edit_line.connect(self._edit_line)
        column.addWidget(self.tabs)
        column.addWidget(self.views, 1)

    def _show_view(self, index: int) -> None:
        self.tabs.select(index)
        self.views.setCurrentIndex(index)
        self.tools.setVisible(index == 0)
        self.setFocusProxy(self.review if index == 1 else self.editor)
        if index == 1:
            self.review.set_lines(self.editor.lines())

    def _edit_line(self, number: int) -> None:
        self._show_view(0)
        self.editor.setTextCursor(QTextCursor(self.editor.document().findBlockByNumber(number)))
        self.editor.setFocus()

    def find(self, query: str | None = None) -> None:
        self._show_view(1)
        if query is not None:
            self.review.search.setText(query)
        self.review.search.setFocus()
        self.review.search.selectAll()

    def _toggle_private(self) -> None:
        self.editor.toggle_private()
        self.save()

    def _note_marks(self, recording_now: bool) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        for row_label in (tr("Decision"), tr("Task"), tr("Question")):  # prefix in the interface language
            button = QPushButton(row_label)
            button.setObjectName("вид")
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setToolTip(
                tr(
                    'Start the line with "{kind} -": the model will put it in that section of the minutes',
                    kind=row_label,
                )
            )
            button.clicked.connect(lambda _=False, v=row_label: self.editor.insert_kind(v))
            row.addWidget(button)
        if recording_now:
            star = QPushButton(style.STAR)
            star.setObjectName("вид")
            star.setStyleSheet('font-family: "Segoe Fluent Icons", "Segoe MDL2 Assets";')
            star.setToolTip(tr("Mark this moment of the recording with a star"))
            star.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            star.clicked.connect(self.mark)
            row.addWidget(star)
        private = QToolButton()
        private.setText(notes.PRIVATE if recording_now else f"{notes.PRIVATE} {tr('Only me')}")
        private.setAccessibleName(tr("Only me"))
        private.setToolTip(tr("Only me · Keep this line out of AI and exports"))
        private.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        private.clicked.connect(self._toggle_private)
        row.addWidget(private)
        row.addStretch()
        if recording_now:
            tooltip = QLabel(tr("Ctrl+Enter - checkbox"))
            tooltip.setObjectName("тихий_текст")
            row.addWidget(tooltip)
        return row

    def load(self) -> None:
        read_count = notes.read(self.folder)
        self.agenda.set_(read_count.agenda)
        self.editor.set_(read_count.lines)
        self._loaded = read_count
        self._modified = False
        if self.review is not None and self.views.currentIndex() == 1:
            self.review.set_lines(read_count.lines)

    def reload_from_disk(self) -> None:
        """Notes may have been written in another window (the recording notepad): reloads if nothing was edited here."""
        if not self._modified:
            self.load()

    def set_agenda(self, items: list[notes.AgendaItem]) -> None:
        self.agenda.set_(items)
        self._later()

    def _later(self) -> None:
        self._modified = True
        self._save_later.start()

    def save(self) -> bool:
        """Saves only its own edits: a notepad where nothing changed does not overwrite a file written by another
        window. A failed write (the file is busy, the disk is full) keeps the text here with the reason and a copy
        outside the meeting folder, the next edit or closing tries again. Returns whether nothing is left unsaved."""
        self._save_later.stop()
        if not self._modified:
            return True
        content = notes.Notes(self.agenda.items(), self.editor.lines())
        try:
            notes.write(self.folder, content, self._loaded)
        except OSError as error:
            log.warning("notes of %s not saved: %s", self.folder.name, error)
            self._keep_copy(failures.text(error), content)
            return False
        self._loaded = notes.from_text(notes.to_text(content))
        self._modified = False
        self.failure.hide()
        if self._spare is not None:  # saved in the meeting: the copy is not needed
            with contextlib.suppress(OSError):
                self._spare.unlink(missing_ok=True)
            self._spare = None
        self.saved.emit()
        return True

    def _keep_copy(self, reason: str, content: notes.Notes) -> None:
        """The reason under the text and a copy of the notes next to the settings: the text survives even if the window
        is closed before a save succeeds."""
        copy_ = settings.data_folder() / SPARE_FOLDER / f"{self.folder.name}.md"
        try:
            copy_.parent.mkdir(parents=True, exist_ok=True)
            copy_.write_text(notes.to_text(content), encoding="utf-8")
        except OSError as error:
            log.warning("copy of the notes of %s not written: %s", self.folder.name, error)
        else:
            self._spare = copy_
            reason = tr("{reason} A copy of the notes: {copy_}", reason=reason, copy_=copy_)
        self.failure.setText(reason)
        self.failure.show()


class LivePanel(QWidget):
    """Transcript during the meeting under the notes: the latest phrases with the recording minute (a click puts a
    phrase into the notes) and a question to the minutes model about what is already recognized.

    Signal chosen(text, second): a phrase for the notes."""

    chosen = Signal(str, float)

    def __init__(self, live_: live.Live):
        super().__init__()
        self.live, self._shown, self._heard, self._asking = live_, -1, live_.others_heard, False
        self._phrases: list[live.Phrase] = []
        heading = QLabel(tr("Now"))
        heading.setObjectName("второй")
        self.status = QLabel()
        self.status.setObjectName("сейчас_ход")
        self.status.setWordWrap(True)
        self.list_ = QListWidget()
        self.list_.setObjectName("сейчас")
        self.list_.setFrameShape(QListWidget.Shape.NoFrame)
        self.list_.setWordWrap(True)
        self.list_.setToolTip(tr("Click a phrase to put it into the notes with its minute"))
        self.list_.itemClicked.connect(self._choose)
        self.question = QLineEdit()
        self.question.setPlaceholderText(tr("Ask about what was said: what did the client promise to send?"))
        self.question.returnPressed.connect(self.ask)
        self.ask_button = QPushButton(tr("Ask"))
        self.ask_button.setObjectName("вид")
        self.ask_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.ask_button.clicked.connect(self.ask)
        self.answer = QLabel()
        self.answer.setWordWrap(True)
        self.answer.setTextFormat(Qt.TextFormat.RichText)
        self.answer.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.answer.linkActivated.connect(self._to_phrase)
        self.answer.hide()
        self._lay_out(heading)
        self.refresh()

    def _lay_out(self, heading: QLabel) -> None:
        top, asking = QHBoxLayout(), QHBoxLayout()
        top.addWidget(heading)
        top.addWidget(self.status, 1)
        asking.addWidget(self.question, 1)
        asking.addWidget(self.ask_button)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 4, 0, 0)
        column.setSpacing(4)
        column.addLayout(top)
        column.addWidget(self.list_, 1)
        column.addLayout(asking)
        column.addWidget(self.answer)

    def refresh(self) -> None:
        """New phrases are added at the bottom; the list follows them unless it was scrolled up."""
        self.status.setText(self.live.status())
        if self.live.version == self._shown:
            return
        self._shown = self.live.version
        bar = self.list_.verticalScrollBar()
        to_end = bar.value() >= bar.maximum() - 2
        phrases, heard = self.live.phrases(), self.live.others_heard
        if heard != self._heard:  # the computer audio has speech now: "Participants" becomes "Me"
            self.list_.clear()
            self._phrases, self._heard = [], heard
        for phrase in phrases[len(self._phrases) :]:
            self.list_.addItem(live.display_line(phrase, heard))
        self._phrases = phrases
        if to_end:
            self.list_.scrollToBottom()

    def _choose(self, item: QListWidgetItem) -> None:
        phrase = self._phrases[self.list_.row(item)]
        self.chosen.emit(live.note_text(phrase, self._heard), phrase.start)

    def ask(self) -> None:
        question = self.question.text().strip()
        if not question or self._asking:
            return
        self._asking = True
        self.ask_button.setText(tr("Asking…"))
        self.ask_button.setEnabled(False)
        run_in_background(QThreadPool.globalInstance(), lambda: self.live.ask(question), self._answered)

    def _answered(self, result: object) -> None:
        """The answer, or why there is none, under the question."""
        self._asking = False
        self.ask_button.setText(tr("Ask"))
        self.ask_button.setEnabled(True)
        failed = isinstance(result, Exception)
        self.answer.setObjectName("ошибка_текст" if failed else "")
        self.answer.setStyleSheet("")  # the style by the new name
        self.answer.setText(html.escape(failures.text(result)) if failed else live.answer_html(str(result), LINK_COLOR))
        self.answer.show()

    def _to_phrase(self, link: str) -> None:
        """A minute in the answer: the phrase said then."""
        number = live.nearest(self._phrases, float(link))
        if number is not None:
            self.list_.setCurrentRow(number)
            self.list_.scrollToItem(self.list_.item(number))


class NotepadWindow(style.DraggedToast):
    """The notepad over the windows during recording: takes focus (it is typed in), hidden during screen sharing,
    moved by the header, resized by the corner. Esc hides it and returns focus to the meeting app.

    Signals: hidden(), place(QRect) when moved or resized, notepad.mark()."""

    hidden = Signal()
    place = Signal(QRect)

    def __init__(
        self,
        folder: pathlib.Path,
        hide_from_capture: bool,
        projects: list[str],
        clock: Callable[[], float],
        live_: live.Live | None = None,
    ):
        """live_: the transcript during this recording, None when it is off."""
        super().__init__(hide_from_capture)
        self.setWindowFlags(style.TOAST_FLAGS & ~Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.setWindowTitle(tr("{name} - notes", name=style.NAME))
        self.setStyleSheet(self.styleSheet() + TOAST_STYLE)
        self._opacity = DENSITY
        self.folder, self._restore = folder, 0
        self.notepad = Notepad(folder, clock, on_toast=True)
        self.live_panel = LivePanel(live_) if live_ is not None else None
        self._time = QLabel()
        self._time.setObjectName("второй")
        self.project = QComboBox()
        self.project.addItem(tr("No project"), "")
        for name in projects:
            self.project.addItem(name, name)
        self._project = catalog.properties(folder).get("проект", "")  # the project whose agenda is in the notepad now
        self.project.setCurrentIndex(max(0, self.project.findData(self._project)))
        self.project.setToolTip(tr("Meeting project: the project agenda comes into the notepad with it"))
        self.project.activated.connect(self._change_project)
        close_view = QToolButton()
        close_view.setText("✕")
        close_view.setToolTip(tr("Hide the notepad, recording goes on  Esc"))
        close_view.clicked.connect(self.hide_away)
        header = QHBoxLayout()
        heading = QLabel(tr("Notes"))
        heading.setObjectName("время")
        for widget in (heading, self._time):
            header.addWidget(widget)
        header.addStretch()
        header.addWidget(self.project)
        header.addWidget(close_view)
        corner = QHBoxLayout()
        corner.addStretch()
        corner.addWidget(QSizeGrip(self), 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        column = QVBoxLayout(self)
        column.setContentsMargins(14, 10, 6, 4)
        column.addLayout(header)
        column.addWidget(self._body(), 1)
        column.addLayout(corner)
        self.setMinimumSize(380, 260)

    def _body(self) -> QWidget:
        """The notepad, and under it the transcript during the meeting when it is on; the border between them moves."""
        if self.live_panel is None:
            return self.notepad
        self.live_panel.chosen.connect(self.notepad.editor.add_line)
        split = QSplitter(Qt.Orientation.Vertical)
        split.setHandleWidth(7)
        split.addWidget(self.notepad)
        split.addWidget(self.live_panel)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        return split

    def event(self, event: object) -> bool:
        result = super().event(event)
        self._opacity = DENSITY  # a toast under the pointer changes its opacity, the notepad does not need that
        return result

    def refresh(self, seconds: float) -> None:
        self._time.setText(transcript.time(seconds))
        if self.live_panel is not None:
            self.live_panel.refresh()

    def display(self) -> None:
        """Shows over the windows with focus at the end of the text; later focus returns to where it was."""
        self._restore = system.window_ahead()
        self.show()
        system.bring_to_front(self)
        self.notepad.editor.setFocus()
        self.notepad.editor.moveCursor(QTextCursor.MoveOperation.End)

    def hide_away(self) -> None:
        """Hides with the notes saved; notes that could not be saved keep it open with the reason."""
        if not self.notepad.save():
            return
        self.hide()
        system.restore_window(self._restore)
        self.hidden.emit()

    def select_project(self, project: str) -> None:
        """The project came from the calendar: selects it in the header and takes its agenda."""
        self.project.setCurrentIndex(max(0, self.project.findData(project)))
        self._change_project()

    def _project_agenda(self, project: str) -> list[notes.AgendaItem]:
        return tasks.agenda(self.folder.parent, project, self.folder) if project else []

    def _change_project(self) -> None:
        """Another project brings its agenda; items added in the notepad and checked ones stay."""
        project, old = self.project.currentData(), self._project
        if project == old:
            return
        self._project = project
        catalog.set_(self.folder, project=project)
        foreign = {p.text for p in self._project_agenda(old)}
        custom_items = [p for p in self.notepad.agenda.items() if p.discussed or p.text not in foreign]
        new_items = [p for p in self._project_agenda(project) if p.text not in {s.text for s in custom_items}]
        self.notepad.set_agenda(custom_items + new_items)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide_away()
        else:
            super().keyPressEvent(event)

    def dropped(self) -> None:
        self.place.emit(self.geometry())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self.isVisible():
            self.place.emit(self.geometry())
