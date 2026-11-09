# SPDX-License-Identifier: GPL-3.0-only
"""Minutes in the meeting window: write them, read them with links to transcript places, edit, copy.

A timestamp in the minutes is a link: a click shows that place in the transcript, which is how the draft is checked.
Sections are shown as blocks with their own shade and a stripe on the left: decisions, tasks and questions stand out.
"""

import html
import logging
import pathlib
import re
from collections.abc import Callable

from PySide6.QtCore import QFileSystemWatcher, QMimeData, QTimer, Qt, Signal
from PySide6.QtGui import QGuiApplication, QTextDocument
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPlainTextEdit,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from knotes import catalog, export, failures, minutes, transcript, system, kinds
from knotes.ui import export_dialog, style
from knotes.language import tr

log = logging.getLogger(__name__)
EMPTY, RUNNING, VIEWING, EDIT = range(4)
SAVE_MS = 700  # an edit of the minutes is saved this long after the last key, as in the notepad
READING_WIDTH = 780  # lines not across the whole window: a long line is hard for the eye to follow
CAPTION_WIDTH = 440  # captions under the "Write minutes" button
SHADES = {
    "зеленый": "99,153,34",
    "синий": "55,138,221",
    "янтарный": "186,117,23",
    "фиолетовый": "127,119,221",
    "красный": "205,75,65",
    "бирюзовый": "24,140,130",
    "серый": "136,135,128",
}  # section shade from its description (R,G,B): left stripe and pale background


def _section_style(section: minutes.Section) -> str:
    """Own pale background and left stripe; a FOR_ANALYST section gets a dashed border: it does not go to the
    client."""
    color = SHADES.get(kinds.description(section.heading).shade, SHADES["серый"])
    edge = (
        f"border: 1px dashed rgba({color},0.6);"
        if minutes.for_analyst(section.heading)
        else f"border: none; border-left: 3px solid rgb({color});"
    )
    return f"QFrame#раздел {{ background: rgba({color},{0.11 if style.dark() else 0.07}); {edge} border-radius: 6px; }}"


def _line(text: str) -> str:
    """Text to HTML: no markup except **bold**."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(text))


def _html_start(text: str) -> str:
    """The heading is large, the email greeting small and gray: people read the results, not the greeting. "> ..."
    notes are not shown: that it is a draft is clear anyway."""
    secondary, quiet = style.text_tones()
    lines = []
    for line in (s.strip() for s in text.splitlines() if s.strip() and not s.startswith(">")):
        if line.startswith("#"):
            lines.append(f"<p style='font-size:20px; font-weight:600; margin:0 0 8px 0'>{_line(line.lstrip('# '))}</p>")
        elif found := minutes.participants(line):
            lines.append(
                f"<p style='margin:6px 0 0 0'><span style='color:{quiet}'>{_line(found[0])}</span>&nbsp; "
                f"{_line(found[1])}</p>"
            )
        else:
            lines.append(f"<p style='color:{secondary}; font-size:12px; margin:0 0 2px 0'>{_line(line)}</p>")
    return "".join(lines)


def _item_html(item: minutes.Item) -> str:
    """Number and time small and gray, the text regular, who and when semibold: the eye finds at once who does
    what."""
    quiet = style.text_tones()[1]
    if not item.number and minutes.transcript_line(item.text):
        return f"<p style='color:{quiet}; font-size:12px; margin:4px 0 0 0'>{_line(item.text)}</p>"
    parts = [f"<span style='color:{quiet}'>{item.number}.</span>&nbsp; " if item.number else "", _line(item.text)]
    if item.who:
        parts.append(f" <span style='font-weight:600'>{_line(item.who)}</span>")
    if item.second is not None:
        parts.append(
            f"&nbsp; <a href='#{item.second}' style='color:{quiet}; text-decoration:none; "
            f"font-size:12px'>{transcript.time(item.second)}</a>"
        )
    if item.from_note:  # as in Granola: shows the item grew from your note, the model wrote the rest
        parts.append(f"&nbsp;<span style='color:{style.accent().name()}; font-size:12px'>{transcript.NOTE}</span>")
    return f"<p style='margin:0 0 8px 0; line-height:135%'>{''.join(parts)}</p>"  # item lines sit closer than items


def email_data(email: str) -> QMimeData:
    """The email for the clipboard: headings and lists for mail, the text as is for plain text. HTML in the minutes
    stays text ("<String>" is not swallowed) and a picture becomes a link: nothing loads by itself in the mail."""
    document = QTextDocument()
    document.setMarkdown(
        re.sub(r"!\[", r"\\![", email),
        QTextDocument.MarkdownFeature.MarkdownDialectGitHub | QTextDocument.MarkdownFeature.MarkdownNoHTML,
    )
    data = QMimeData()
    data.setHtml(document.toHtml())
    data.setText(minutes.no_diarization(email))
    return data


def read_problem(error: Exception) -> str:
    """Why the minutes file could not be read, in words."""
    if isinstance(error, UnicodeDecodeError):
        return tr("The minutes file is not in UTF-8: open it in Notepad and save it as UTF-8.")
    return failures.text(error)


class MinutesView(QWidget):
    """Signals: compose() to write the minutes or write them again; ask_assistant() to have them written by the
    user's own AI assistant; to_place(second) to show a place in the transcript."""

    compose = Signal()
    ask_assistant = Signal()
    to_place = Signal(float)

    def __init__(self, folder: pathlib.Path, source_of: Callable[[], str]):
        super().__init__()
        self.folder, self._source_fn = folder, source_of
        self._busy = False
        self._modified = False  # the edit differs from the file
        self.save_problem = ""  # why the last save failed
        self._save_later = QTimer(self, singleShot=True, interval=SAVE_MS)
        self._save_later.timeout.connect(self.save)
        self.pages = QStackedWidget()
        for page in (self._empty(), self._running(), self._view_page(), self._editing()):
            self.pages.addWidget(page)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addLayout(self._buttons())
        column.addWidget(self.pages, 1)
        self._watcher = QFileSystemWatcher([str(folder)], self)  # minutes saved by the AI assistant from outside
        self._watcher.directoryChanged.connect(self._file_changed)
        self._watcher.fileChanged.connect(self._file_changed)
        self.display()

    @property
    def file(self) -> pathlib.Path:
        return self.folder / minutes.FILE

    def _buttons(self) -> QHBoxLayout:
        self._copy_button = QPushButton(tr("Copy"))
        self._copy_button.setToolTip(
            tr(
                "Without timestamps and the analyst-only section: email - for mail, for knowledge base - without the "
                "greeting"
            )
        )
        menu = QMenu(self._copy_button)
        menu.addAction(tr("Email"), self._copy_email)
        menu.addAction(tr("For knowledge base"), self._copy_for_knowledge_base)
        menu.addSeparator()
        menu.addAction(tr("Save as file (PDF, Word)…"), self._save_file)
        self._copy_button.setMenu(menu)
        self._change = QPushButton(tr("Change"))
        self._change.clicked.connect(self._toggle_edit)
        self.result = QLabel()
        self.result.setTextFormat(Qt.TextFormat.PlainText)  # a service refusal text is not markup
        self.result.setObjectName("тихий_текст")
        self._more = style.more_button(
            [
                (tr("Write again"), self._compose_again),
                (tr("Ask your AI assistant"), self.ask_assistant.emit),
                (tr("Open file"), lambda: system.open_text(self.file, False)),
            ]
        )
        row = QHBoxLayout()
        for widget in (self._copy_button, self._change):
            row.addWidget(widget)
        row.addStretch()
        row.addWidget(self.result)
        row.addWidget(self._more)
        return row

    def _empty(self) -> QWidget:
        self._missing = QLabel(tr("No minutes yet"))
        self._missing.setObjectName("группа")
        self._button = QPushButton(tr("Write minutes"))
        self._button.setObjectName("главная")
        self._button.clicked.connect(lambda: self._compose(self.meeting_kind.currentData()))
        self.meeting_kind = QComboBox()
        for number, kind_ in enumerate(kinds.kinds()):
            self.meeting_kind.addItem(
                tr(kind_.name), kind_.name
            )  # data is the kind name in Russian, as in the meeting properties
            self.meeting_kind.setItemData(number, tr(kind_.description), Qt.ItemDataRole.ToolTipRole)
        self.meeting_kind.setToolTip(
            tr("Meeting type: it sets the sections of the minutes. By default - the project's type")
        )
        row = QWidget()
        window_layout = QHBoxLayout(row)
        window_layout.setContentsMargins(0, 0, 0, 0)
        window_layout.addWidget(self.meeting_kind)
        window_layout.addWidget(self._button)
        self._destination = style.WrappedCaption("", CAPTION_WIDTH)
        self._destination.setObjectName("тихий_текст")
        self._error_label = style.WrappedCaption("", CAPTION_WIDTH)
        self._error_label.setObjectName("ошибка_текст")
        self._assistant_link = style.WrappedCaption(
            tr("or <a href='#'>ask your own AI assistant</a> (Claude Desktop and others, over MCP)"),
            CAPTION_WIDTH,
            rich=True,
        )
        self._assistant_link.setObjectName("тихий_текст")
        self._assistant_link.linkActivated.connect(lambda _link: self.ask_assistant.emit())
        return self._centered(self._missing, row, self._destination, self._assistant_link, self._error_label)

    def _running(self) -> QWidget:
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setFixedWidth(200)
        bar.setTextVisible(False)
        row_label = QLabel(
            tr("Writing the minutes, usually 1-2 minutes. You can close the window: the draft saves itself.")
        )
        row_label.setObjectName("второй_текст")
        row_label.setWordWrap(True)
        row_label.setMinimumWidth(240)
        row_label.setMaximumWidth(480)
        row_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        return self._centered(row_label, bar)

    @staticmethod
    def _centered(*widgets: QWidget) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.addStretch()
        for widget in widgets:
            column.addWidget(widget, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addStretch(2)
        return page

    def _view_page(self) -> QWidget:
        self.sheet = QWidget()
        self.sheet.setMaximumWidth(READING_WIDTH)
        self._sheet_column = QVBoxLayout(self.sheet)
        self._sheet_column.setContentsMargins(4, 4, 12, 4)
        self._sheet_column.setSpacing(10)
        self._paging = QScrollArea()
        self._paging.setWidgetResizable(True)
        self._paging.setFrameShape(QFrame.Shape.NoFrame)
        self._paging.setWidget(self.sheet)
        self.setFocusProxy(self._paging)  # arrows and Page Down scroll the minutes
        return self._paging

    def repaint_theme(self) -> None:
        """Block and quiet text colors follow the theme: open minutes are laid out again."""
        if self.pages.currentIndex() == VIEWING and (text := self._read()) is not None:
            self._lay_out(text)

    def _read(self) -> str | None:
        """The minutes file; None when there is none or it cannot be read (the reason is shown)."""
        try:
            return self.file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            log.warning("minutes not read: %s", error)
            self._error_label.setText(read_problem(error))
            return None

    def _lay_out(self, text: str) -> None:
        """The start of the minutes as text, each section as its own block; a FOR_ANALYST section is set farther
        from the rest."""
        style.empty(self._sheet_column)
        start, parts = minutes.sections(text)
        if start:
            self._sheet_column.addWidget(self._caption(_html_start(start)))
        for section in parts:
            if minutes.for_analyst(section.heading):
                self._sheet_column.addSpacing(14)
            self._sheet_column.addWidget(self._section(section))
        self._sheet_column.addStretch()

    def _caption(self, text: str) -> QLabel:
        """Item text: timestamps are links, the text can be selected with the mouse to copy a piece."""
        caption = QLabel(text)
        caption.setTextFormat(Qt.TextFormat.RichText)
        caption.setWordWrap(True)
        caption.setStyleSheet("font-size: 14px;")
        caption.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        caption.linkActivated.connect(self._by_link)
        return caption

    def _section(self, section: minutes.Section) -> QFrame:
        """Section block: semibold heading, the item count in gray next to it, the items below."""
        secondary, quiet = style.text_tones()
        block = QFrame()
        block.setObjectName("раздел")
        block.setStyleSheet(_section_style(section))
        heading = style.caption(section.heading)
        heading.setObjectName("заголовок_раздела")
        heading.setStyleSheet("font-size: 14px; font-weight: 600;")
        header = QHBoxLayout()
        header.addWidget(heading)
        if section.item_count:
            count = QLabel(str(section.item_count))
            count.setObjectName("пунктов")
            count.setStyleSheet(f"color: {quiet}; font-size: 12px;")
            header.addWidget(count)
        header.addStretch()
        column = QVBoxLayout(block)
        column.setContentsMargins(14, 10, 12, 6)
        column.addLayout(header)
        if section.text:
            text = self._caption("".join(_item_html(p) for p in minutes.items(section)))
            text.setObjectName("текст_раздела")
            if minutes.for_analyst(section.heading):
                text.setStyleSheet(f"font-size: 13px; color: {secondary};")
            column.addWidget(text)
        return block

    def _editing(self) -> QWidget:
        self.edit_item = QPlainTextEdit()
        self.edit_item.setFrameShape(QFrame.Shape.NoFrame)
        self.edit_item.textChanged.connect(self._edited)
        return self.edit_item

    def _edited(self) -> None:
        self._modified = True
        self._save_later.start()

    # --- state ----------------------------------------------------------------------------------------------

    def display(self, error: str = "") -> None:
        self._error_label.setText("")
        text = None if self._busy else self._read()
        exists = text is not None
        if self._busy:
            self.pages.setCurrentIndex(RUNNING)
        elif text is not None:
            self._lay_out(text)
            self.edit_item.blockSignals(True)  # the file itself is not an edit to save
            self.edit_item.setPlainText(text)
            self.edit_item.blockSignals(False)
            self._modified = False
            self._save_later.stop()
            self.pages.setCurrentIndex(VIEWING)
        else:
            recognized = (self.folder / transcript.TRANSCRIPT).exists()
            self._button.setEnabled(recognized)
            self.meeting_kind.setEnabled(recognized)
            self.meeting_kind.setCurrentIndex(max(0, self.meeting_kind.findData(kinds.meeting_kind(self.folder).name)))
            self._destination.setText(self._source_fn() if recognized else tr("Transcribe the meeting first"))
            self.pages.setCurrentIndex(EMPTY)
        error = error or self._error_label.text()
        self._error_label.setText(error)
        self._error_label.setVisible(bool(error) and not exists)
        for button in (self._copy_button, self._change):
            button.setVisible(exists and not self._busy)
        self._more.setVisible(self.file.exists() and not self._busy)
        self._change.setText(tr("Change"))
        self.result.setText(error if exists else "")  # price and model are in the summary, per meeting

    def _file_changed(self, _path: str) -> None:
        """The minutes file appeared or changed outside the program: show it, unless the user is editing."""
        if self.file.exists() and str(self.file) not in self._watcher.files():
            self._watcher.addPath(str(self.file))
        if not self._busy and self.pages.currentIndex() != EDIT:
            self.display()

    def stop_watching(self) -> None:
        """The meeting closes: its folder is let go (a watched folder can't go to the Windows Recycle Bin)."""
        if paths := self._watcher.files() + self._watcher.directories():
            self._watcher.removePaths(paths)

    def assistant_asked(self) -> None:
        text = tr(
            "Request copied. Paste it to your AI assistant (Claude Desktop and others): the minutes will appear "
            "here by themselves."
        )
        self._destination.setText(text)
        self.result.setText(text)

    def busy(self, yes: bool) -> None:
        self.save()
        self._busy = yes
        self.display()

    # --- actions --------------------------------------------------------------------------------------------

    def save(self) -> bool:
        """Writes the edit to the file: while typing, when editing is done and when the window closes. False: it
        could not be written; the edit stays in the field, the reason is shown and kept in save_problem."""
        self._save_later.stop()
        text = self.edit_item.toPlainText()
        if not (self._modified and self.pages.currentIndex() == EDIT and text.strip()):
            return True
        try:
            with transcript.locked(self.file):  # the AI assistant may be saving the minutes at this moment
                transcript.write_text(self.file, text.rstrip() + "\n")
        except OSError as error:
            log.warning("minutes edit not saved: %s", error)
            self.save_problem = failures.text(error)
            self.result.setText(tr("Not saved: {reason}", reason=self.save_problem))
            return False
        self._modified, self.save_problem = False, ""
        return True

    def _toggle_edit(self) -> None:
        if self.pages.currentIndex() == EDIT:
            if self.save():
                self.display()
            return
        self.pages.setCurrentIndex(EDIT)
        self._change.setText(tr("Done"))
        self.edit_item.setFocus()

    def _compose(self, kind_: str) -> None:
        """The kind is saved in the meeting: it is used to write the minutes again and from the AI assistant."""
        catalog.set_(self.folder, kind=kind_)
        self.compose.emit()

    def _compose_again(self) -> None:
        names = [v.name for v in kinds.kinds()]
        row_labels = [tr(name) for name in names]
        choice, yes = QInputDialog.getItem(
            self,
            tr("Write the minutes again"),
            tr("The draft and your edits in it will be replaced with a new draft.\nMeeting type"),
            row_labels,
            names.index(kinds.meeting_kind(self.folder).name),
            False,
        )
        if yes:
            self._compose(names[row_labels.index(choice)])

    def _current_text(self) -> str | None:
        """What to copy: the edit being typed (it is saved too), otherwise the file; None when it cannot be read."""
        if self.pages.currentIndex() == EDIT:
            self.save()
            return self.edit_item.toPlainText()
        text = self._read()
        if text is None:
            self._report(self._error_label.text() or tr("No minutes yet"))
        return text

    def _copy_email(self) -> None:
        if (text := self._current_text()) is None:
            return
        QGuiApplication.clipboard().setMimeData(email_data(minutes.for_email(text)))
        self._report(tr("Email copied, paste it into your mail"))

    def _copy_for_knowledge_base(self) -> None:
        if (text := self._current_text()) is None:
            return
        QGuiApplication.clipboard().setText(minutes.for_knowledge_base(text))
        self._report(tr("Copied for the knowledge base"))

    def _save_file(self) -> None:
        """The minutes for the client as a file: the same text as the email."""
        if (text := self._current_text()) is None:
            return
        document = export.minutes_document(text, catalog.display_title(self.folder))
        if said := export_dialog.save_as(self, document, export.file_name(tr("Протокол"), self.folder)):
            self._report(said)

    def _report(self, text: str) -> None:
        self.result.setText(text)
        QTimer.singleShot(3000, self.result.clear)

    def _by_link(self, address: str) -> None:
        second = address.removeprefix("#")
        if second.isdigit():
            self.to_place.emit(float(second))


class InstructionDialog(QDialog):
    """Minutes instructions: what and how the model should write. "Restore default" brings back the program's
    instruction."""

    def __init__(self, parent_: QWidget | None = None):
        super().__init__(parent_)
        self.setWindowTitle(tr("Minutes instructions - {name}", name=style.NAME))
        self.resize(820, 640)
        self.text = QPlainTextEdit(minutes.instruction())
        default_text = QPushButton(tr("Restore default"))
        default_text.clicked.connect(
            lambda: self.text.setPlainText(minutes.DEFAULT_INSTRUCTION.read_text(encoding="utf-8"))
        )
        cancel, save = QPushButton(tr("Cancel")), QPushButton(tr("Save"))
        save.setObjectName("главная")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addWidget(default_text)
        bottom.addStretch()
        bottom.addWidget(cancel)
        bottom.addWidget(save)
        column = QVBoxLayout(self)
        column.addWidget(self.text, 1)
        column.addLayout(bottom)
        self.setStyleSheet(style.window_style())

    def accept(self) -> None:
        minutes.set_instruction(self.text.toPlainText())
        super().accept()
