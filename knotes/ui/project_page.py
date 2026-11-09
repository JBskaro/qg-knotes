# SPDX-License-Identifier: GPL-3.0-only
"""The project page: right of the meetings list when a project is selected and no meeting is open.

Preparation for a meeting (like talking points in Fellow): the analyst's questions and topics; the minutes model can
suggest questions for the client and a checklist for before the meeting (prep.py). From them, the questions in "About
the project", what was not discussed last time and the open questions of the board, the agenda is assembled; it can be
copied to the participants and it also comes into the meeting notepad. Below are the lasting project details and the
notes of all project meetings.
"""

import html
import pathlib

from PySide6.QtCore import Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from knotes import notes, tasks, catalog, projects, prep, failures, minutes, settings
from knotes.ui import style
from knotes.ui.background import run_in_background
from knotes.language import tr

SNIPPET_LINES = 2
_GONE = object()
# AI prep requests in flight by (recordings, project): the page to get the answer, None if it closed (the answer goes
# to the saved prep). A page rebuilt meanwhile takes the answer over; a second click does not ask again.
_preparing: dict[tuple[pathlib.Path, str], "ProjectPage | None"] = {}


def _prepared(key: tuple[pathlib.Path, str], result: object) -> None:
    page = _preparing.pop(key, _GONE)
    if isinstance(page, ProjectPage):
        page.show_prepared(result)
    elif page is None and isinstance(result, prep.Suggestion):
        prep.add_saved(*key, result.lines)


class ProjectPage(QWidget):
    """Signals: open_notes(folder): meeting notes from the feed; board(project): the project task board."""

    open_notes = Signal(pathlib.Path)
    board = Signal(str)

    def __init__(self, recordings: pathlib.Path, project: str):
        super().__init__()
        self.recordings, self.project = recordings, project
        self._key = (recordings, project)
        self._changed_all: set[str] = set()  # what was edited here and not saved yet: questions, details
        self._save_later = QTimer(self, singleShot=True, interval=700)
        self._save_later.timeout.connect(self.save)
        meetings = [v for v in catalog.list_(recordings) if v.project == project]
        project_tasks = tasks.tasks(recordings, project)
        self._past = prep.past(meetings, project_tasks)  # read once: an edit of the prep does not read every meeting
        self.sheet = QWidget()
        self.sheet.setMaximumWidth(780)
        column = QVBoxLayout(self.sheet)
        column.setContentsMargins(8, 4, 12, 12)
        column.setSpacing(8)
        column.addLayout(self._header(len(meetings), sum(1 for z in project_tasks if z.opened)))
        for widget in (
            *self._preparation(),
            *self._about_project(),
            self._heading(tr("Заметки встреч")),
            self._feed(meetings),
        ):
            column.addWidget(widget)
        column.addStretch()
        paging = QScrollArea()
        paging.setWidgetResizable(True)
        paging.setFrameShape(QFrame.Shape.NoFrame)
        paging.setWidget(self.sheet)
        QVBoxLayout(self).addWidget(paging)
        self._show_agenda()
        if self._key in _preparing:  # the page was rebuilt while the model is answering: the answer comes here
            _preparing[self._key] = self
            self._busy(True)

    @staticmethod
    def _heading(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-size: 14px; font-weight: 600; margin-top: 10px;")
        return label

    def _header(self, meetings_count: int, open_count: int) -> QHBoxLayout:
        name = style.caption(self.project)
        name.setStyleSheet("font-size: 20px; font-weight: 600;")
        account = QLabel(" · ".join((tr("meetings: {n}", n=meetings_count), tr("open items: {n}", n=open_count))))
        account.setObjectName("второй_текст")
        board = QPushButton(tr("Project board"))
        board.clicked.connect(lambda: self.board.emit(self.project))
        row = QHBoxLayout()
        row.addWidget(name)
        row.addWidget(account, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch()
        row.addWidget(board)
        return row

    def _preparation(self) -> list[QWidget]:
        self.questions = QPlainTextEdit(projects.for_meeting(self.recordings, self.project))
        self.questions.setPlaceholderText(
            tr(
                'What to ask and discuss at the next meeting, one line per agenda item. Questions from "About the '
                'project" and open questions from the board are added automatically'
            )
        )
        self.questions.setFixedHeight(110)
        self.questions.setObjectName("поле_записи")
        self.questions.textChanged.connect(lambda: self._changed_item("вопросы"))
        self._prepare_button = QPushButton(tr("Prepare questions"))
        self._prepare_button.setToolTip(
            tr(
                "The minutes model suggests questions for the client by the meeting kind and a checklist of what to "
                'request before the meeting. It gets "About the project", the prep and open questions of past meetings'
            )
        )
        self._prepare_button.clicked.connect(self._prepare)
        self._prepare_button.setMinimumWidth(self._prepare_button.sizeHint().width())  # "Preparing…" is shorter
        self._prepare_state = QLabel()
        self._prepare_state.setObjectName("тихий_текст")
        self._prepare_state.setWordWrap(True)
        self.agenda = QLabel()
        self.agenda.setWordWrap(True)
        self.agenda.setTextFormat(Qt.TextFormat.RichText)
        copy = QPushButton(tr("Copy agenda"))
        copy.setToolTip(tr("Agenda as text - send it to participants before the meeting"))
        copy.clicked.connect(self._copy_button)
        self._copied = QLabel()
        self._copied.setObjectName("тихий_текст")
        row_label = QLabel(tr("Next meeting agenda - it will appear in the notepad when you start recording"))
        row_label.setObjectName("тихий_текст")
        return [
            self._heading(tr("Meeting prep")),
            self.questions,
            self._row(self._prepare_button, self._prepare_state),
            row_label,
            self.agenda,
            self._row(copy, self._copied),
        ]

    @staticmethod
    def _row(button: QPushButton, state: QLabel) -> QWidget:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(button)
        row_layout.addWidget(state, 1)
        return row

    def _about_project(self) -> list[QWidget]:
        info = projects.notes(self.recordings, self.project)
        self.info = QPlainTextEdit(info.text)
        self.info.setPlaceholderText(
            tr(
                "Lasting facts about the project: database and version, agreements, terms, who is who. Questions here "
                "also go to the agenda"
            )
        )
        self.info.setFixedHeight(130)
        self.info.setObjectName("поле_записи")
        self.info.textChanged.connect(lambda: self._changed_item("сведения"))
        self.for_model = QCheckBox(tr("Use this when writing the minutes"))
        self.for_model.setChecked(info.for_model)
        self.for_model.toggled.connect(lambda: self._changed_item("сведения"))
        return [self._heading(tr("About the project")), self.info, self.for_model]

    def _feed(self, meetings: list[catalog.Meeting]) -> QWidget:
        """Project meetings with notes, newest first: date, title, how many lines and the first lines."""
        feed = QWidget()
        column = QVBoxLayout(feed)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(10)
        secondary, quiet = style.text_tones()
        for meeting in (v for v in meetings if v.folder.is_dir()):
            lines = [s.text.strip() for s in notes.read(meeting.folder).lines if s.text.strip()]
            if not lines:
                continue
            snippet = "<br>".join(html.escape(s) for s in lines[:SNIPPET_LINES])
            caption = QLabel(
                f"<a href='#' style='text-decoration:none'>{meeting.date:%d.%m} · "
                f"{html.escape(meeting.display_title)}</a> <span style='color:{quiet}'>· "
                f"{tr('lines: {n}', n=len(lines))}</span><br><span style='color:{secondary}'>"
                f"{snippet}</span>"
            )
            caption.setWordWrap(True)
            caption.linkActivated.connect(lambda _link, p=meeting.folder: self.open_notes.emit(p))
            column.addWidget(caption)
        if not column.count():
            empty = QLabel(tr("No notes yet. While recording, open the notepad with the pencil on the recording bar"))
            empty.setObjectName("тихий_текст")
            empty.setWordWrap(True)
            column.addWidget(empty)
        return feed

    def _changed_item(self, field: str) -> None:
        self._changed_all.add(field)
        self._save_later.start()

    def save(self) -> None:
        """Only what was edited here: questions discussed at a meeting left the file, the page does not return them."""
        self._save_later.stop()
        changed, self._changed_all = self._changed_all, set()
        if "вопросы" in changed:
            projects.set_for_meeting(self.recordings, self.project, self.questions.toPlainText())
        if "сведения" in changed:
            projects.set_(
                self.recordings,
                self.project,
                projects.ProjectNotes(self.info.toPlainText(), self.for_model.isChecked()),
            )
        if changed:  # closing or copying without edits does not lay out the agenda again
            self._show_agenda()

    def editing(self) -> bool:
        """Someone is typing here or there is unsaved text: do not rebuild the page."""
        return bool(self._changed_all) or self.isAncestorOf(QApplication.focusWidget())

    def close_without_saving(self) -> None:
        """The project was renamed or deleted: the old name must not come back in the project details, nor with the
        model's answer."""
        self._changed_all.clear()
        self._save_later.stop()
        if _preparing.get(self._key) is self:
            del _preparing[self._key]
        self.close()

    def _items(self) -> list[notes.AgendaItem]:
        """The agenda from the texts on the page and the past read once (as tasks.agenda builds it from the files)."""
        return prep.agenda(self.questions.toPlainText(), self.info.toPlainText(), self._past)

    def _show_agenda(self) -> None:
        items = self._items()
        self.agenda.setText(
            "".join(f"<p style='margin:0 0 4px 0'>{n}.&nbsp; {html.escape(p.text)}</p>" for n, p in enumerate(items, 1))
            or f"<span style='color:{style.text_tones()[1]}'>{tr('No items yet')}</span>"
        )

    def _copy_button(self) -> None:
        self.save()
        QGuiApplication.clipboard().setText(
            notes.agenda_text(self._items(), tr("Meeting agenda, project {project}", project=self.project))
        )
        self._copied.setText(tr("Copied"))
        QTimer.singleShot(3000, self._copied.clear)

    # --- questions from the model --------------------------------------------------------------------------------

    def _prepare(self) -> None:
        """The request goes in the background with the prep as typed; a second click while the model answers does
        not ask again, also from a page rebuilt meanwhile."""
        if self._key in _preparing:
            return
        self.save()
        _preparing[self._key] = self
        self._busy(True)
        recordings, project = self._key
        run_in_background(
            QThreadPool.globalInstance(),
            lambda: prep.suggest(recordings, project, minutes.params(settings.load())),
            lambda result, key=self._key: _prepared(key, result),
        )

    def _busy(self, yes: bool) -> None:
        self._prepare_button.setEnabled(not yes)
        self._prepare_button.setText(tr("Preparing…") if yes else tr("Prepare questions"))
        if yes:
            self._say("")

    def _say(self, text: str, error: bool = False) -> None:
        self._prepare_state.setObjectName("ошибка_текст" if error else "тихий_текст")
        self._prepare_state.setStyleSheet("")  # the name changed: restyle by the new name
        self._prepare_state.setText(text)

    def show_prepared(self, result: object) -> None:
        """The model's lines go under the analyst's text, which stays as is (Ctrl+Z takes them back); a failure is
        shown in plain words and the prep is not touched."""
        self._busy(False)
        if isinstance(result, BaseException):  # the background job wrote the details to the log
            self._say(failures.text(result), error=True)
            return
        suggestion: prep.Suggestion = result  # type: ignore[assignment]
        added = prep.addition(self.questions.toPlainText(), suggestion.lines, [p.text for p in self._items()])
        if not added:
            self._say(tr("Nothing new: the suggested questions are already in the prep or the agenda"))
            return
        cursor = QTextCursor(self.questions.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(added)  # one undo step; the edit is saved as typing is
        result_text = tr("Added to the prep: {n}. Edit them as you need", n=len(added.strip().splitlines()))
        self._say(" · ".join((result_text, catalog.money(suggestion.cost))) if suggestion.cost else result_text)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.save()
        if _preparing.get(self._key) is self:  # the answer goes to the saved prep or to the project's next page
            _preparing[self._key] = None
        super().closeEvent(event)
