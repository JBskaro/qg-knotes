# SPDX-License-Identifier: GPL-3.0-only
"""Meeting view: title and project, the tabs "Minutes", "Notes" (the analyst's notepad) and "Transcript" (text by
participant, search, names, marks and notes among the phrases).

Consecutive phrases of one person go under one name. A click on the time plays that place, a double click on a
phrase opens it for editing. A click on a time in the minutes or in the notes opens the transcript at that place.
"""

import html
import logging
import pathlib
import re
from collections.abc import Callable
from typing import cast

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QFocusEvent, QKeyEvent, QKeySequence, QMouseEvent, QResizeEvent, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from knotes import notes, catalog, export, failures, meeting_list, player, transcript, screen, system
from knotes.export import participant_caption
from knotes.ui import export_dialog, style
from knotes.ui.notepad import Notepad
from knotes.ui.minutes_view import MinutesView
from knotes.ui.transcript_scroll import TranscriptScroll
from knotes.ui.video_view import VideoView
from knotes.player import Player
from knotes.language import tr

log = logging.getLogger(__name__)
HIGHLIGHT_MS = 2500  # how long a phrase found from the minutes stays highlighted
FONT_SIZES = (8, 16)  # the usual Windows font is 9, A− must go smaller than that too
PANEL_WIDTH = 220
MINUTES, NOTES, TRANSCRIPT = "протокол", "заметки", "расшифровка"  # meeting tabs
SEARCH_MS = 200  # the transcript is searched once typing pauses, not on every letter
FULL_WIDTH, FROM_FULL, PEOPLE = "", "", ""  # Windows font icons: full screen, restore the window, people


class PhraseEdit(QPlainTextEdit):
    """Phrase edit field in place of the text: Enter or leaving the field saves, Esc cancels."""

    done = Signal(str)
    cancel = Signal()

    def __init__(self, text: str):
        super().__init__(text)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().contentsChanged.connect(self._by_text)
        self._closed_flag = False

    def _by_text(self) -> None:
        self.setFixedHeight(int(self.document().size().height() * self.fontMetrics().lineSpacing()) + 14)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers():
            self._close_tracks(True)
        elif event.key() == Qt.Key.Key_Escape:
            self._close_tracks(False)
        else:
            super().keyPressEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        self._close_tracks(True)

    def _close_tracks(self, save: bool) -> None:
        if not self._closed_flag:
            self._closed_flag = True
            if save:
                self.done.emit(self.toPlainText())
            else:
                self.cancel.emit()


class PhraseRow(QLabel):
    """Selectable phrase text with a playback button in its left margin."""

    play = Signal(object)  # the row itself
    fixed = Signal(int, str)
    note = Signal(float)  # add a note to this phrase: its start
    TIME_WIDTH = 76

    def __init__(self, number: int, start: float, text: str, has_audio: bool):
        super().__init__()
        self.number, self.start, self.phrase_text = number, start, text
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setContentsMargins(self.TIME_WIDTH, 1, 0, 1)
        self.setToolTip(tr("Double-click to edit the text, right-click to add a note here"))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.time = QToolButton(self)
        self.time.setObjectName("время_фразы")
        self.time.setAutoRaise(True)
        self.time.setFixedWidth(self.TIME_WIDTH - 4)
        self.time.setToolTip(tr("Play from here") if has_audio else tr("Audio deleted, can't play"))
        self.time.setEnabled(has_audio)
        self.time.clicked.connect(lambda: self.play.emit(self))
        self.setMinimumHeight(self.time.sizeHint().height() + 2)
        self._editor: PhraseEdit | None = None
        self.playing = False
        self._plain = False  # the label shows the text without highlights
        self.show_time(False)
        self.light_up(None, -1)

    def show_time(self, hovered: bool) -> None:
        sign = style.STOP if self.playing else style.LATER_SPAN
        self.time.setText(f"{sign if hovered or self.playing else ' '} {transcript.time(self.start)}")

    def enterEvent(self, event: QEvent) -> None:
        self.show_time(self.time.isEnabled())
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self.show_time(False)
        super().leaveEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._editor is not None:
            width = max(1, self.width() - self.TIME_WIDTH)
            if self._editor.width() != width:
                self._editor.setFixedWidth(width)
                self._size_editor(self._editor)

    def _size_editor(self, field: PhraseEdit) -> None:
        field._by_text()
        self.setMinimumHeight(max(self.time.sizeHint().height() + 2, field.height() + 2))

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and event.position().x() >= self.TIME_WIDTH:
            self._edit()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def light_up(self, query: re.Pattern | None, current_value: int) -> int:
        """Highlight matches; avoid redrawing phrases without matches."""
        pale, bright = style.HIGHLIGHT[style.dark()]
        marked, found = (
            style.highlighted(self.phrase_text, query, lambda n: bright if n == current_value else pale)
            if query
            else ("", 0)
        )
        if not found:
            if not self._plain:
                self.setText(html.escape(self.phrase_text))
                self._plain = True
            return 0
        self._plain = False
        self.setText(marked)
        return found

    def _menu(self, point: QPoint) -> None:
        menu = QMenu(self)
        menu.addAction(tr("Add a note here…"), lambda: self.note.emit(self.start))
        menu.addAction(tr("Edit text"), self._edit)
        menu.exec(self.mapToGlobal(point))

    def _edit(self) -> None:
        if self._editor is not None:
            return
        rendered = self.text()
        field = PhraseEdit(self.phrase_text)
        field.setParent(self)
        field.setFont(self.font())
        field.document().documentLayout().documentSizeChanged.connect(lambda _: self._size_editor(field))
        field.setFixedWidth(max(1, self.width() - self.TIME_WIDTH))
        field.move(self.TIME_WIDTH, 1)
        self._editor = field
        self.setText("")
        field.done.connect(lambda text: self._edit_result(field, text, rendered))
        field.cancel.connect(lambda: self._edit_result(field, self.phrase_text, rendered))
        field.show()
        self._size_editor(field)
        field.setFocus()
        field.selectAll()

    def _edit_result(self, field: PhraseEdit, text: str, rendered: str) -> None:
        field.hide()
        field.deleteLater()
        self._editor = None
        self.setMinimumHeight(self.time.sizeHint().height() + 2)
        self.setText(rendered)
        if text.strip() and text.strip() != self.phrase_text:
            self.phrase_text = text.strip()
            self._plain = False
            self.light_up(None, -1)
            self.fixed.emit(self.number, self.phrase_text)


PAGE_ROWS = 24


def _transcript_pages(blocks: list[dict], only: str | None) -> list[list[dict]]:
    """Split even a single long turn into small groups without repeating the speaker's name."""
    pages: list[list[dict]] = []
    remaining = 0
    for block in blocks:
        if only is not None and block.get("кто") != only:
            continue
        if "метка" in block:
            if not remaining:
                pages.append([])
                remaining = PAGE_ROWS
            pages[-1].append(block)
            remaining -= 1
            continue
        phrases = block["фразы"]
        start = 0
        while start < len(phrases):
            if not remaining:
                pages.append([])
                remaining = PAGE_ROWS
            part = phrases[start : start + remaining]
            pages[-1].append({"кто": block["кто"], "фразы": part, "continued": start > 0})
            remaining -= len(part)
            start += len(part)
    return pages


class TranscriptPage(QWidget):
    def __init__(self):
        super().__init__()
        self.column = QVBoxLayout(self)
        self.column.setContentsMargins(0, 0, 0, 0)
        self.rows: dict[int, PhraseRow] = {}
        self.marks: dict[int, QWidget] = {}
        self.chips: list[tuple[str, QLabel]] = []


class MeetingView(QWidget):
    """Signals: recognize(folder) - "Transcribe again"; meeting_changed() - the title or project changed;
    compose_minutes(folder). minutes_source() - where and to which model the transcript goes, for the button caption."""

    recognize = Signal(pathlib.Path)
    meeting_changed = Signal()
    compose_minutes = Signal(pathlib.Path)
    ask_assistant = Signal(pathlib.Path)  # the minutes by the user's own AI assistant: copy the request to it
    expand = Signal()  # read at full window width: hide or bring back the projects and the list
    closed = Signal()  # the meeting closed: the program forgets it at once, not when Qt deletes the widget
    tab_shown = Signal(str)  # a tab shown after opening: the next meeting opens on it
    _played_out = Signal(str)

    def __init__(
        self,
        folder: pathlib.Path,
        in_notepad: bool = False,
        projects: list[str] | None = None,
        minutes_source: Callable[[], str] = lambda: "",
        tab: str = MINUTES,
    ):
        """tab: the one to open on, as the previous meeting had it; one this meeting does not have yet gives way
        to the closest one."""
        super().__init__()
        self.folder, self.in_notepad, self._minutes_source = folder, in_notepad, minutes_source
        self._transcript_blocks: list[dict] = []
        self._phrase_data: dict[int, tuple[float, str]] = {}
        self._page_data: list[list[dict]] = []
        self._phrase_pages: dict[int, int] = {}
        self._mark_pages: dict[int, int] = {}
        self._speaker_names: dict[str, str] = {}
        self._audio_available = False
        self._pattern: re.Pattern | None = None
        self._match_offsets: dict[int, int] = {}
        self._participants: list[str] = []
        self._built: tuple | None = None  # versions of the files the shown transcript was built from
        self._only: str | None = None
        self._current, self._found = 0, 0
        self._size_of = QApplication.font().pointSize()  # phrase size in points, changed by A− and A+
        self._playing: int | None = None
        self.player = Player()
        self._played_out.connect(self._playback_end)
        self._build(projects or [], tab)
        self.repaint_theme()
        self.refresh()
        self.setFocus()  # not in the title field: otherwise stray typing would rename the meeting

    # --- layout ---------------------------------------------------------------------------------------------

    def _build(self, projects: list[str], tab: str) -> None:
        """Under the header: the minutes tab or the transcript tab with participants, each at full width."""
        column = QVBoxLayout(self)
        column.setContentsMargins(6, 4, 4, 4)
        column.addLayout(self._header(projects))
        self.scroll_area = TranscriptScroll(self._create_page, self._keep_page)
        self.text = self.scroll_area.canvas
        self._text_field = self.scroll_area.viewport()  # for eventFilter: on close the scroll area may be deleted
        self._text_field.installEventFilter(self)
        self.panel = QWidget()
        self.panel.setFixedWidth(PANEL_WIDTH)
        self._panel_column = QVBoxLayout(self.panel)
        self._participants_edge = QFrame()
        self._participants_edge.setObjectName("разделитель")
        self._participants_edge.setFixedWidth(1)
        text_and_participants = QHBoxLayout()
        text_and_participants.addWidget(self.scroll_area, 1)
        text_and_participants.addWidget(self._participants_edge)
        text_and_participants.addWidget(self.panel)
        self.transcript_panel = QWidget()
        transcript_column = QVBoxLayout(self.transcript_panel)
        transcript_column.setContentsMargins(0, 0, 0, 0)
        self.video = VideoView(self.folder)
        transcript_column.addWidget(self.video, 0, Qt.AlignmentFlag.AlignHCenter)
        transcript_column.addLayout(self._search_panel())
        transcript_column.addLayout(text_and_participants, 1)
        self.minutes = MinutesView(self.folder, self._minutes_source)
        self.minutes.compose.connect(lambda: self.compose_minutes.emit(self.folder))
        self.minutes.ask_assistant.connect(lambda: self.ask_assistant.emit(self.folder))
        self.minutes.to_place.connect(self.show_place)
        self.notepad = Notepad(self.folder)
        self.notepad.to_place.connect(self.show_place)
        self.contents_ = QStackedWidget()
        self._pages = {MINUTES: self.minutes, NOTES: self.notepad, TRANSCRIPT: self.transcript_panel}
        for page in self._pages.values():
            self.contents_.addWidget(page)
        column.addWidget(self.contents_, 1)
        try:
            self.show_tab(self._closest_tab(tab))
        except (OSError, ValueError) as error:  # ValueError: a file not in UTF-8; the meeting opens anyway
            log.warning("meeting %s opens on the minutes, its transcript was not built: %s", self.folder.name, error)
            self.show_tab(MINUTES)
        self.show_participants(False)

    def _closest_tab(self, tab: str) -> str:
        """A meeting not transcribed yet has no transcript: it opens on the minutes, which say to transcribe it
        first. The minutes and the notes are there for every meeting."""
        if tab == TRANSCRIPT and not (self.folder / transcript.TRANSCRIPT).exists():
            return MINUTES
        return tab if tab in self._pages else MINUTES

    def _header(self, projects: list[str]) -> QVBoxLayout:
        self.title = QLineEdit()
        self.title.setObjectName("название_встречи")
        self.title.setFrame(False)
        font = self.title.font()
        font.setPointSizeF(font.pointSizeF() * 1.25)
        font.setBold(True)
        self.title.setFont(font)
        self.title.setToolTip(tr("Meeting title: click to change"))
        self.title.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # by click only: stray typing will not rename it
        self.title.editingFinished.connect(self._change_title)
        self.project = QComboBox()
        self.project.setEditable(True)
        self.project.addItems(["", *projects])
        self.project.lineEdit().setPlaceholderText(tr(catalog.NO_PROJECT))
        self.project.setMinimumWidth(150)
        self.project.setMaximumWidth(220)
        self.project.lineEdit().editingFinished.connect(self._change_project)
        self.project.activated.connect(self._change_project)
        self.tabs = style.Segments(
            [(MINUTES, tr("Протокол")), (NOTES, tr("Notes")), (TRANSCRIPT, tr("Transcript"))], MINUTES
        )
        self.tabs.setToolTip(tr("Minutes, your notes or the transcript; switch minutes and transcript  Ctrl+3"))
        self.tabs.selected.connect(self.show_tab)
        self.full_width_button = style.icon_button(FULL_WIDTH, "", self.expand.emit)
        self.full_width(False)
        top = QHBoxLayout()
        top.addWidget(self.title, 1)
        top.addWidget(self.tabs)
        top.addWidget(self.full_width_button)
        self.summary = QLabel()
        self.summary.setObjectName("второй_текст")
        self.problem = style.caption("")
        self.problem.setObjectName("ошибка_текст")
        self.problem.hide()
        details = QHBoxLayout()  # the project next to the date: the top line stays short in a narrow column
        details.addWidget(self.project)
        details.addWidget(self.summary, 1)
        header = QVBoxLayout()
        header.setSpacing(2)
        header.addLayout(top)
        header.addLayout(details)
        header.addWidget(self.problem)
        return header

    def _search_panel(self) -> QHBoxLayout:
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Find in transcript  Ctrl+F"))
        self.search.setClearButtonEnabled(True)
        self.search.addAction(style.icon(style.SEARCH, "#888780"), QLineEdit.ActionPosition.LeadingPosition)
        self._search_delay = QTimer(self, singleShot=True, interval=SEARCH_MS)
        self._search_delay.timeout.connect(lambda: self._light_up(0))
        self.search.textChanged.connect(lambda: self._search_delay.start())
        self.search.returnPressed.connect(self._enter)
        self.found = QLabel()
        self.found.setObjectName("второй_текст")
        self.found.setMinimumWidth(56)
        row = QHBoxLayout()
        row.addWidget(self.search, 1)
        row.addWidget(self.found)
        self._arrows: list[QToolButton] = []  # visible only with text in the search: otherwise nothing to step
        for sign, tooltip, action in (
            (style.UP, tr("Previous  Shift+Enter"), lambda: self._step(-1)),
            (style.DOWN, tr("Next  Enter"), lambda: self._step(1)),
            ("A−", tr("Smaller  Ctrl+mouse wheel"), lambda: self._font_size(-1)),
            ("A+", tr("Larger  Ctrl+mouse wheel"), lambda: self._font_size(1)),
        ):
            button = style.icon_button(sign, tooltip)
            if sign.startswith("A"):  # letters in the usual font: font size icons are not in every Windows
                button.setObjectName("буквы")
            else:
                button.hide()
                self._arrows.append(button)
            button.clicked.connect(action)
            row.addWidget(button)
        self.participants_button = style.icon_button(PEOPLE, tr("Participants: names and who spoke how much"))
        self.participants_button.setCheckable(True)
        self.participants_button.toggled.connect(self.show_participants)
        row.addWidget(self.participants_button)
        file, video = self.folder / transcript.TRANSCRIPT, self.folder / screen.VIDEO_FILE
        row.addWidget(
            style.more_button(
                [
                    (
                        tr("Open in Notepad") if self.in_notepad else tr("Open file"),
                        lambda: system.open_text(file, self.in_notepad),
                    ),
                    *([(tr("Open the video"), lambda: system.open_view(video))] if video.is_file() else []),
                    (tr("Save as file (PDF, Word)…"), self._save_file),
                    (tr("Open folder"), lambda: system.open_view(self.folder)),
                    (tr("Transcribe again"), lambda: self.recognize.emit(self.folder)),
                ]
            )
        )
        for key_seq, action in (
            (QKeySequence.StandardKey.Find, self._to_search),
            ("Shift+Return", lambda: self._step(-1)),
            ("F3", lambda: self._step(1)),
        ):
            QShortcut(QKeySequence(key_seq), self, action).setContext(
                Qt.ShortcutContext.WidgetWithChildrenShortcut
            )  # the meeting view sits inside the meetings window
        return row

    def repaint_theme(self) -> None:
        self.setStyleSheet(
            style.window_style() + "QToolButton#время_фразы { border: none; padding: 1px 4px; "
            "font-size: 12px; min-width: 64px; text-align: left; }"
        )
        self.minutes.repaint_theme()

    # --- contents -------------------------------------------------------------------------------------------

    def refresh(self) -> None:
        """Everything anew: after a rename, an edit, a repeated transcription. The transcript is built when its tab
        is shown, and again only when its files changed."""
        meeting = catalog.meeting(self.folder)
        self.window().setWindowTitle(f"{catalog.display_title(self.folder)} - {style.NAME}")
        self.title.setText(catalog.display_title(self.folder))
        self.title.setCursorPosition(0)  # a title longer than the field shows its beginning
        self.project.setEditText(meeting.project if meeting else "")
        self.summary.setText(self._summary())
        self.video.reload()
        if self.tab() == TRANSCRIPT:
            self._build_transcript()

    def _sources(self) -> tuple:
        """What the transcript on screen is built from: the phrases, names, marks, notes and whether there is audio."""
        versions = []
        for name in (transcript.PHRASES, transcript.NAMES, transcript.MARKS, notes.FILE):
            try:
                info = (self.folder / name).stat()
            except OSError:
                versions.append(None)
            else:
                versions.append((info.st_mtime_ns, info.st_size))
        meeting = catalog.meeting(self.folder)
        return *versions, bool(meeting and meeting.has_audio)

    def _build_transcript(self) -> None:
        sources = self._sources()
        if sources == self._built:
            return
        self._finish_phrase_edit()
        position = self.scroll_area.verticalScrollBar().value()
        self.player.stop()
        self._playing = None
        self._participants = transcript.participants(self.folder)
        self._speaker_names, self._audio_available = transcript.names(self.folder), sources[-1]
        self._transcript_blocks = transcript.blocks(self.folder, notes.with_minutes(self.folder))
        self._phrase_data = {
            number: (start, text) for block in self._transcript_blocks for number, start, text in block.get("фразы", ())
        }
        self._load_pages(position)
        self._participants_panel(self._speaker_names)
        self._built = sources

    @property
    def _rows(self) -> list[PhraseRow]:
        return [row for page in self._mounted_pages() for row in page.rows.values()]

    def _mounted_pages(self) -> list[TranscriptPage]:
        mounted = self.scroll_area.mounted
        return [cast(TranscriptPage, mounted[index]) for index in sorted(mounted)]

    def _load_pages(self, position: int) -> None:
        self._page_data = _transcript_pages(self._transcript_blocks, self._only) or [[]]
        self._phrase_pages.clear()
        self._mark_pages.clear()
        estimates = []
        for index, blocks in enumerate(self._page_data):
            height = 24 if not blocks else 0
            for block in blocks:
                if "метка" in block:
                    self._mark_pages[id(block)] = index
                    height += 34
                else:
                    phrases = block["фразы"]
                    self._phrase_pages.update((number, index) for number, _, _ in phrases)
                    height += 25 * len(phrases) + (0 if block["continued"] else 34)
            estimates.append(height)
        self.scroll_area.set_pages(estimates, position)
        self._light_up(self._current, jump=False)

    def _create_page(self, index: int) -> QWidget:
        page = TranscriptPage()
        blocks = self._page_data[index]
        for block in blocks:
            if "метка" in block:
                self._add_label(page, block)
            else:
                self._add_block(page, block)
        if not blocks:
            page.column.addWidget(QLabel(tr("The meeting isn't transcribed yet")))
        return page

    def _keep_page(self, widget: QWidget) -> bool:
        focus = QApplication.focusWidget()
        return bool((focus and widget.isAncestorOf(focus)) or (self._playing and widget.isAncestorOf(self._playing)))

    def _summary(self) -> str:
        """When and how long it lasted; service, models and costs are in the summary, by meeting."""
        data = transcript.phrases(self.folder) or {"сведения": {}}
        parts = [catalog.folder_date(self.folder).strftime("%d.%m.%Y, %H:%M")]
        if data["сведения"].get("длительность"):
            parts.append(catalog.duration(data["сведения"]["длительность"]))
        return " · ".join(parts)

    def _add_block(self, page: TranscriptPage, block: dict) -> None:
        widget = QWidget()
        column = QVBoxLayout(widget)
        column.setContentsMargins(0, 0 if block["continued"] else 6, 0, 6)
        column.setSpacing(1)
        who, participants = block["кто"], self._participants
        if not block["continued"]:
            name = style.caption(self._speaker_names.get(who) or participant_caption(who))
            name.setStyleSheet(style.participant_chip(participants.index(who) if who in participants else 0))
            page.chips.append((who, name))
            column.addWidget(name, alignment=Qt.AlignmentFlag.AlignLeft)
        for number, _, _ in block["фразы"]:
            start, text = self._phrase_data[number]
            row = PhraseRow(number, start, text, self._audio_available)
            row.play.connect(self._play)
            row.fixed.connect(self._fix)
            row.note.connect(self._note_at)
            page.rows[number] = row
            if number == self._playing:
                row.playing = True
                row.show_time(True)
            self._highlight_row(row)
            column.addWidget(row)
        page.column.addWidget(widget)

    def _add_label(self, page: TranscriptPage, block: dict) -> None:
        """A ★ mark or a ✎ note among the phrases; right-click to delete."""
        note = block["метка"].startswith(transcript.NOTE)
        label = QFrame()
        label.setObjectName("метка")
        label.setToolTip(
            tr("Your note: the model will use it in the minutes. Right-click to delete")
            if note
            else tr("Mark: the star was pressed here during recording, to find this moment quickly later")
        )
        label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        label.customContextMenuRequested.connect(lambda point: self._label_menu(block, label.mapToGlobal(point)))
        row = QHBoxLayout(label)
        row.setContentsMargins(70, 3, 0, 3)
        icon = QLabel(style.NOTE if note else style.STAR)
        icon.setStyleSheet(
            f'font-family: "Segoe Fluent Icons", "Segoe MDL2 Assets"; '
            f"color: {style.accent().name() if note else style.state_color('ждет')[0]};"
        )
        text = (
            (tr(block["метка"]) if block["метка"] == transcript.MARKER else block["метка"])
            .removeprefix(transcript.NOTE if note else "★")
            .strip()
        )
        row_label = style.caption(f"{transcript.time(block['время'])}  {text}")
        row_label.setObjectName("" if note else "второй_текст")
        row_label.setWordWrap(True)
        row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(row_label, 1)
        page.marks[id(block)] = label
        page.column.addWidget(label)

    def _label_menu(self, block: dict, where: QPoint) -> None:
        menu = QMenu(self)
        menu.addAction(tr("Delete"), lambda: self._delete_label(block))
        menu.exec(where)

    def _delete_label(self, block: dict) -> None:
        """A note is removed from the meeting notepad, a mark from the transcript."""
        if block["метка"].startswith(transcript.NOTE):
            self.notepad.save()
            notes.delete(self.folder, block["время"], block["метка"].removeprefix(transcript.NOTE))
            self.notepad.load()
        else:
            transcript.delete_label(self.folder, block["время"], block["метка"])
        self.refresh()

    def _note_at(self, seconds: float) -> None:
        """A note to a phrase after the meeting: the time is the phrase start; it goes to the meeting notepad too."""
        text, yes = QInputDialog.getText(
            self, tr("Note"), tr("Note at {time}: what matters here", time=transcript.time(seconds))
        )
        if yes and text.strip():
            self.notepad.save()
            notes.add(self.folder, seconds, text)
            self.notepad.load()
            self.refresh()

    # --- participants and marks panel -----------------------------------------------------------------------

    def _participants_panel(self, names: dict[str, str]) -> None:
        style.empty(self._panel_column)
        self._panel_column.addWidget(self._panel_title(tr("Participants")))
        shares = transcript.shares(self.folder)
        for number, who in enumerate(self._participants):
            self._panel_column.addWidget(self._participant(number, who, names.get(who, ""), shares.get(who, 0.0)))
        marks = [block for block in self._transcript_blocks if "метка" in block]
        if marks:
            self._panel_column.addWidget(self._panel_title(tr("Marks and notes")))
            for block in marks:
                self._panel_column.addWidget(self._label_link(block))
        self._panel_column.addStretch()

    @staticmethod
    def _panel_title(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("группа")
        return label

    def _participant(self, number: int, who: str, name: str, share: float) -> QWidget:
        widget = QWidget()
        column = QVBoxLayout(widget)
        column.setContentsMargins(0, 2, 0, 8)
        column.setSpacing(4)
        text, _background = style.participant_color(number)
        point = QLabel("●")
        point.setStyleSheet(f"color: {text};")
        row_label = style.caption(participant_caption(who))
        row_label.setObjectName("тихий_текст")
        filter_item = style.icon_button(style.FILTER_FILES, tr("Show only their lines"))
        filter_item.setCheckable(True)
        filter_item.setChecked(self._only == who)
        filter_item.toggled.connect(lambda on: self._filter_fn(who if on else None))
        header = QHBoxLayout()
        for part in (point, row_label):
            header.addWidget(part)
        header.addStretch()
        header.addWidget(filter_item)
        column.addLayout(header)
        if who == "Я":
            self_ = QLabel(tr("This is you, no name needed"))
            self_.setObjectName("второй_текст")
            column.addWidget(self_)
        else:
            field = QLineEdit(name)
            field.setPlaceholderText(tr("Type a name and press Enter"))
            field.editingFinished.connect(lambda: self._rename(who, field.text()))
            column.addWidget(field)
        bar = QProgressBar()
        bar.setObjectName("доля")
        bar.setTextVisible(False)
        bar.setValue(round(share * 100))
        bar.setToolTip(tr("Spoke about {n}% of the time", n=round(share * 100)))
        bar.setStyleSheet(f"QProgressBar::chunk {{ background: {text}; border-radius: 2px; }}")
        column.addWidget(bar)
        return widget

    def _label_link(self, block: dict) -> QPushButton:
        seconds, text = block["время"], block["метка"]
        note = text.startswith(transcript.NOTE)
        row_label = text.removeprefix(transcript.NOTE).strip()[:24].replace("&", "&&") if note else ""  # not a mnemonic
        button = QPushButton(f"{style.NOTE if note else style.STAR}  {transcript.time(seconds)}  {row_label}")
        button.setFlat(True)
        button.setToolTip(style.plain_tooltip(text) if note else tr("Go to the mark set during recording"))
        button.setStyleSheet('font-family: "Segoe UI Variable Text", "Segoe Fluent Icons"; text-align: left;')
        button.clicked.connect(lambda: self._to_mark(id(block)))
        return button

    # --- actions --------------------------------------------------------------------------------------------

    def _change_title(self) -> None:
        name = self.title.text().strip()
        if name == catalog.display_title(self.folder):
            return
        catalog.set_(self.folder, title=catalog.for_storage(self.folder, name))
        self.window().setWindowTitle(f"{catalog.display_title(self.folder)} - {style.NAME}")
        self.meeting_changed.emit()

    def _change_project(self) -> None:
        project = self.project.currentText().strip()
        if project != (catalog.properties(self.folder).get("проект") or ""):
            catalog.set_(self.folder, project=project)
            self.meeting_changed.emit()

    def _rename(self, who: str, name: str) -> None:
        """The new name goes over the participant's blocks: the transcript is not built again for it."""
        names = transcript.names(self.folder)
        if names.get(who, "") == name.strip():
            return
        transcript.set_names(self.folder, {who: name})  # only this one: another may have changed meanwhile
        self._speaker_names = {**names, who: name.strip()}
        for page in self._mounted_pages():
            for chip_of, chip in page.chips:
                if chip_of == who:
                    chip.setText(name.strip() or participant_caption(who))
        self._built = self._sources()

    def _fix(self, number: int, text: str) -> None:
        transcript.fix_phrase(self.folder, number, text)
        start, _ = self._phrase_data[number]
        self._phrase_data[number] = start, text
        self._built = self._sources()  # the row shows the new text already
        self._light_up(self._current, jump=False)

    def _filter_fn(self, who: str | None) -> None:
        self._finish_phrase_edit()
        self._only = who
        self._participants_panel(self._speaker_names)
        self._load_pages(0)

    def _finish_phrase_edit(self) -> None:
        focus = QApplication.focusWidget()
        if focus and self.text.isAncestorOf(focus):
            self.scroll_area.setFocus()

    def _stop_phrase_icon(self, number: int | None) -> None:
        index = self._phrase_pages.get(number) if number is not None else None
        if index is not None and index in self.scroll_area.mounted:
            page = cast(TranscriptPage, self.scroll_area.mounted[index])
            row = page.rows[number]
            row.playing = False
            row.show_time(False)

    def _play(self, row: PhraseRow) -> None:
        old, self._playing = self._playing, None
        self.player.stop()
        self.video.stop()
        self._stop_phrase_icon(old)
        if old == row.number:
            return
        self._playing, row.playing = row.number, True
        row.show_time(True)
        self.player.play(self.folder, row.start, self._played_out.emit)
        self.video.play(row.start, player.LENGTH_S)  # frames play along with the phrase sound

    def _playback_end(self, error: str) -> None:
        self.video.stop()
        self.problem.setText(error)
        self.problem.setVisible(bool(error))
        self._stop_phrase_icon(self._playing)
        self._playing = None

    # --- search and font ------------------------------------------------------------------------------------

    def _enter(self) -> None:
        """Enter right after typing searches at once; then it goes to the next match."""
        if self._search_delay.isActive():
            self._search_delay.stop()
            self._light_up(0)
        else:
            self._step(1)

    def _highlight_row(self, row: PhraseRow) -> None:
        offset = self._match_offsets.get(row.number)
        row.light_up(self._pattern if offset is not None else None, self._current - (offset or 0))

    def _light_up(self, current_value: int, jump: bool = True) -> None:
        query = self.search.text().strip()
        self._pattern = meeting_list.search_pattern(query) if query else None
        self._match_offsets = {}
        total = 0
        if self._pattern:
            for number in self._phrase_pages:
                found = sum(1 for _ in self._pattern.finditer(self._phrase_data[number][1]))
                if found:
                    self._match_offsets[number] = total
                    total += found
        self._found, self._current = total, min(current_value, max(0, total - 1))
        for row in self._rows:
            self._highlight_row(row)
        self.found.setText(
            tr("{number} of {total}", number=self._current + 1, total=total)
            if total
            else (tr("not found") if query else "")
        )
        for arrow in self._arrows:
            arrow.setVisible(bool(query))
        if jump and total:
            number = next(n for n, offset in reversed(self._match_offsets.items()) if offset <= self._current)
            self._to_phrase(number)

    def _step(self, on_: int) -> None:
        if self._found:
            self._light_up((self._current + on_) % self._found)

    def _font_size(self, on_: int) -> None:
        """Phrase size as a style sheet rule: the window style sheet overrides a font set with setFont."""
        self._size_of = min(FONT_SIZES[1], max(FONT_SIZES[0], self._size_of + on_))
        self.text.setStyleSheet(f"QLabel {{ font-size: {self._size_of}pt; }}")

    def eventFilter(self, subject: QObject, event: QEvent) -> bool:
        if (
            subject is self._text_field
            and event.type() == QEvent.Type.Wheel
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self._font_size(1 if event.angleDelta().y() > 0 else -1)
            return True
        return super().eventFilter(subject, event)

    # --- minutes --------------------------------------------------------------------------------------------

    def minutes_in_progress(self, yes: bool, error: str = "") -> None:
        self.minutes.busy(yes)
        if error:
            self.minutes.display(error)

    def full_width(self, yes: bool) -> None:
        """The icon shows what a click does: expand the meeting or bring back the projects and the list."""
        self.full_width_button.setText(FROM_FULL if yes else FULL_WIDTH)
        self.full_width_button.setToolTip(
            tr("Bring back projects and the meeting list  F11")
            if yes
            else tr("Read full width: hide projects and the list  F11")
        )

    def tab(self) -> str:
        page = self.contents_.currentWidget()
        return next(name for name, widget in self._pages.items() if widget is page)

    def show_tab(self, name: str) -> None:
        """Keys go to the open tab, not the title field: otherwise stray typing would rename the meeting. Going from
        the notes to the transcript shows the new notes among the phrases."""
        if name == TRANSCRIPT and self.contents_.currentWidget() is self.notepad:
            self._save_notes()
        if name == NOTES:  # during recording this meeting's notes are written in the notepad on top of the windows
            self.notepad.reload_from_disk()
        if name == TRANSCRIPT:
            self._build_transcript()
        self.contents_.setCurrentWidget(self._pages[name])
        self.tabs.select(name)
        self.setFocusProxy({TRANSCRIPT: self.scroll_area, NOTES: self.notepad.editor}.get(name, self.minutes))
        if self.contents_.isVisible():
            self.setFocus()  # arrows and the wheel scroll the open tab
        self.tab_shown.emit(name)

    def switch_tab(self) -> None:
        self.show_tab(MINUTES if self.tab() == TRANSCRIPT else TRANSCRIPT)

    def _to_search(self) -> None:
        """Ctrl+F searches the transcript whichever tab it is pressed on."""
        self.show_tab(TRANSCRIPT)
        self.search.setFocus()

    def _to_phrase(self, number: int) -> PhraseRow:
        index = self._phrase_pages[number]
        page = cast(TranscriptPage, self.scroll_area.page(index))
        row = page.rows[number]
        self.scroll_area.reveal(index, row)
        return row

    def _to_mark(self, mark: int) -> None:
        if self._only is not None:
            self._filter_fn(None)
        if mark in self._mark_pages:
            index = self._mark_pages[mark]
            page = cast(TranscriptPage, self.scroll_area.page(index))
            self.scroll_area.reveal(index, page.marks[mark])

    def show_participants(self, on: bool) -> None:
        """Participants panel (names, speech shares, marks) by a button: when open, it squeezes the transcript text."""
        for part in (self.panel, self._participants_edge):
            part.setVisible(on)
        self.participants_button.blockSignals(True)
        self.participants_button.setChecked(on)
        self.participants_button.blockSignals(False)

    def show_place(self, seconds: float) -> None:
        """A place from the minutes on the transcript tab: the phrase at that time is highlighted (the first one that
        starts no earlier). Back to the minutes with its tab or Ctrl+3, the minutes stay where they were read."""
        self.show_tab(TRANSCRIPT)
        if self._only is not None:
            self._filter_fn(None)
        if not self._phrase_data:
            return
        last = next(reversed(self._phrase_data))
        number = next((n for n, (start, _) in self._phrase_data.items() if int(start) >= seconds), last)
        row = self._to_phrase(number)
        self.video.show_frame(row.start)
        row.setStyleSheet(f"background: {style.HIGHLIGHT[style.dark()][0]}; border-radius: 4px;")
        QTimer.singleShot(HIGHLIGHT_MS, row, lambda: row.setStyleSheet(""))

    def _save_file(self) -> None:
        """The transcript as a file: title, date, participants, phrases with their time and speaker."""
        if transcript.phrases(self.folder) is None:
            self.problem.setText(tr("The meeting isn't transcribed yet"))
            self.problem.show()
            return
        name = export.file_name(tr("Transcript"), self.folder)
        if said := export_dialog.save_as(self, export.transcript_document(self.folder), name):
            self.found.setText(said)
            QTimer.singleShot(4000, self.found, lambda: self._light_up(self._current))  # back to the search count

    def _save_notes(self) -> str:
        """Saves the notes; why they could not be saved, empty if they were."""
        try:
            self.notepad.save()
        except OSError as error:
            log.warning("meeting notes not saved: %s", error)
            self.problem.setText(tr("Notes not saved: {reason}", reason=failures.text(error)))
            self.problem.show()
            return failures.text(error)
        return ""

    def closeEvent(self, event: QCloseEvent) -> None:
        """The minutes edit and the notes are saved; what cannot be saved is asked about, not lost silently. The
        program is told the meeting closed whatever happens while saving: then a click opens it again."""
        try:
            problem = " ".join(p for p in (self._save_minutes(), self._save_notes()) if p)
            if problem and not self._close_anyway(problem):
                event.ignore()
                return
            self.player.stop()
            self.video.stop()
            self.minutes.stop_watching()
            super().closeEvent(event)
        finally:
            if event.isAccepted():
                self.closed.emit()

    def _save_minutes(self) -> str:
        return "" if self.minutes.save() else self.minutes.save_problem

    def _close_anyway(self, problem: str) -> bool:
        answer = QMessageBox.question(
            self,
            tr("Close without saving?"),
            tr("{reason} Close the meeting without saving the changes?", reason=problem),
        )
        return answer == QMessageBox.StandardButton.Yes
