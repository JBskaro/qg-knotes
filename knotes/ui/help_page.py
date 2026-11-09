# SPDX-License-Identifier: GPL-3.0-only
"""The "Help" section: questions by topic (knotes/help_topics.py), a click opens the answer as steps; the search box
at the top of the window finds answers by the start of a word, in questions and answers."""

import html
import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from knotes.help_topics import TOPICS, Answer
from knotes.language import tr
from knotes.ui import style
from knotes.ui.settings_data import SECTIONS

COLUMN_WIDTH = 680  # about 80 characters a line: a wider line is hard to follow with the eye
SNIPPET_BEFORE, SNIPPET_AFTER = 50, 90


def _plain(text: str) -> str:
    return text.replace("**", "")


def _escape(text: str) -> str:
    """As HTML; a dash does not start a line: "Ссылка - секрет" breaks after the dash, not before it."""
    return html.escape(text, quote=False).replace(" - ", "&nbsp;- ")


def _markup(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", _escape(text))


def answer_html(answer: Answer) -> str:
    """The answer as rich text: steps or facts, the quiet note and the link to the settings section."""
    tag = "ol" if answer.steps else "ul"
    items = "".join(f"<li style='margin-bottom: 8px;'>{_markup(tr(line))}</li>" for line in answer.lines)
    parts = [f"<{tag} style='-qt-list-indent: 1; margin-top: 0;'>{items}</{tag}>"]
    if answer.note:
        parts.append(f"<p style='color: {style.text_tones()[1]}; margin-top: 6px;'>{_markup(tr(answer.note))}</p>")
    if answer.settings:
        label = tr("Open “{section}” in Settings", section=tr(SECTIONS[answer.settings]))
        link = f"<a href='{answer.settings}' style='text-decoration: none;'>{html.escape(label)}</a>"
        parts.append(f"<p style='margin-top: 6px;'>{link}</p>")
    return "".join(parts)


def _patterns(query: str) -> list[re.Pattern[str]]:
    """Each word of the query from the start of a word: "ключ" finds "ключа", not "включите"."""
    return [re.compile(rf"(?<!\w){re.escape(word)}", re.IGNORECASE) for word in query.split()]


def _body(answer: Answer) -> str:
    return _plain(" ".join(tr(text) for text in (*answer.lines, answer.note) if text))


def found(answer: Answer, query: str) -> bool:
    """All words of the query are in the question or the answer."""
    text = f"{_plain(tr(answer.question))} {_body(answer)}"
    return all(pattern.search(text) for pattern in _patterns(query))


def snippet(answer: Answer, query: str) -> str:
    """A piece of the answer around the first found word, the words in bold."""
    body = _body(answer)
    hits = [match.start() for pattern in _patterns(query) if (match := pattern.search(body))]
    at = min(hits, default=0)
    begin = body.rfind(" ", 0, at - SNIPPET_BEFORE) + 1 if at > SNIPPET_BEFORE else 0
    end = body.find(" ", at + SNIPPET_AFTER)
    piece = _escape(body[begin : end if end > 0 else len(body)])
    words = "|".join(re.escape(html.escape(word, quote=False)) for word in query.split())
    color = style.text_tones()[0]
    marked = re.sub(rf"(?<!\w)({words})", rf"<b style='color: {color};'>\1</b>", piece, flags=re.IGNORECASE)
    return ("…" if begin else "") + marked + ("…" if end > 0 else "")


class Question(QWidget):
    """A question row; a click opens its answer under it, a search shows a piece of the answer."""

    link = Signal(str)

    def __init__(self, answer: Answer, parent_: QWidget):
        super().__init__(parent_)
        self.answer, self.opened = answer, False
        self.header = QPushButton(tr(answer.question), self)
        self.header.setObjectName("вопрос")
        self.header.clicked.connect(lambda: self.set_open(not self.opened))
        self.piece = QLabel(self)
        self.piece.setObjectName("тихий_текст")
        self.piece.setWordWrap(True)
        self.piece.setContentsMargins(36, 0, 10, 6)
        self.body = QLabel(self)
        self.body.setObjectName("ответ")
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.RichText)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.body.setContentsMargins(34, 2, 10, 12)
        self.body.linkActivated.connect(self.link.emit)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        for widget in (self.header, self.piece, self.body):
            column.addWidget(widget)
        self.set_open(False)

    def set_open(self, opened: bool) -> None:
        self.opened = opened
        if opened and not self.body.text():
            self.body.setText(answer_html(self.answer))
        self.body.setVisible(opened)
        self.piece.setVisible(not opened and bool(self.piece.text()))
        self.header.setIcon(style.icon(style.DOWN if opened else style.RIGHT, style.text_tones()[0]))
        font = self.header.font()
        font.setBold(opened)
        self.header.setFont(font)

    def repaint_theme(self) -> None:
        """The arrow and the quiet note of an opened answer are drawn in the colors of the theme."""
        if self.body.text():
            self.body.setText(answer_html(self.answer))
        self.set_open(self.opened)

    def show_piece(self, text: str) -> None:
        self.piece.setText(text)
        self.piece.setVisible(bool(text) and not self.opened)


class HelpPage(QWidget):
    """The "Help" section. Signals: tips_again and check_sound from the "Getting started" buttons,
    open_settings(section) from a link in an answer. The search box at the top of the window calls search(query)."""

    tips_again = Signal()
    check_sound = Signal()
    open_settings = Signal(str)

    def __init__(self, parent_: QWidget | None = None):
        super().__init__(parent_)
        self.topic = 0
        self.questions: list[tuple[int, Question]] = []
        self.headings: list[QLabel] = []
        body = QWidget(self)
        column = QVBoxLayout(body)
        column.setContentsMargins(28, 22, 28, 28)
        column.setSpacing(0)
        title = QLabel(tr("Help"), body)
        title.setObjectName("заголовок_страницы")
        self.lead = QLabel(body)
        self.lead.setObjectName("тихий_текст")
        self.lead.setWordWrap(True)
        self.lead.setContentsMargins(0, 4, 0, 14)
        self.segments = self._segments(body)
        for widget in (title, self.lead, self.segments):
            column.addWidget(widget)
        column.addSpacing(10)
        self._fill(body, column)
        self.start = self._start(body)
        column.addWidget(self.start)
        column.addStretch(1)
        body.setMaximumWidth(COLUMN_WIDTH + 56)
        self._lay_out(body)
        self.show_topic(0)

    def repaint_theme(self) -> None:
        for _topic, question in self.questions:
            question.repaint_theme()

    @property
    def search_hint(self) -> str:
        return tr("Search help  Ctrl+F")

    def _segments(self, parent_: QWidget) -> QWidget:
        """Topics as one row of segments, like the period switch in the summary."""
        row = QWidget(parent_)
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(0)
        self.topics = QButtonGroup(self)
        for number, (topic, _answers) in enumerate(TOPICS):
            button = QPushButton(tr(topic), row)
            last = len(TOPICS) - 1
            kind = "сегмент_первый" if number == 0 else "сегмент_последний" if number == last else "сегмент"
            button.setObjectName(kind)
            button.setCheckable(True)
            self.topics.addButton(button, number)
            line.addWidget(button)
        line.addStretch(1)
        self.topics.idClicked.connect(self.show_topic)
        return row

    def _fill(self, parent_: QWidget, column: QVBoxLayout) -> None:
        """All questions in topic order, each topic under its heading; a topic or a search only shows or hides them."""
        for number, (topic, answers) in enumerate(TOPICS):
            heading = QLabel(tr(topic), parent_)
            heading.setObjectName("тема")
            self.headings.append(heading)
            column.addWidget(heading)
            for answer in answers:
                question = Question(answer, parent_)
                question.link.connect(self.open_settings.emit)
                self.questions.append((number, question))
                column.addWidget(question)

    def _start(self, parent_: QWidget) -> QWidget:
        block = QWidget(parent_)
        column = QVBoxLayout(block)
        column.setContentsMargins(0, 26, 0, 0)
        heading = QLabel(tr("Getting started"), block)
        heading.setObjectName("тема")
        again = QPushButton(tr("Show tips again"), block)
        again.clicked.connect(self.tips_again.emit)
        sound = QPushButton(tr("Check sound and recognition"), block)
        sound.clicked.connect(self.check_sound.emit)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(10, 0, 0, 0)
        buttons.setSpacing(8)
        buttons.addWidget(again)
        buttons.addWidget(sound)
        buttons.addStretch(1)
        column.addWidget(heading)
        column.addLayout(buttons)
        return block

    def _lay_out(self, body: QWidget) -> None:
        """The column of text on the left of a scrolling card: on a wide window the lines stay short."""
        holder = QWidget(self)
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(body, 1)
        row.addStretch(0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(holder)
        card = style.layer(scroll, padding=0)
        card.setParent(self)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(card)

    def show_topic(self, number: int) -> None:
        """The questions of one topic, closed answers stay as they were."""
        self.topic = number
        self.topics.button(number).setChecked(True)
        self.lead.setText(tr("Choose a topic and click a question. To search all of the help, type in the box above."))
        self.segments.show()
        self.start.show()
        for heading in self.headings:
            heading.hide()
        for topic, question in self.questions:
            question.show_piece("")
            question.setVisible(topic == number)

    def search(self, query: str) -> None:
        """Answers with all words of the query, by topic; an empty query goes back to the topic."""
        query = query.strip()
        if not query:
            self.show_topic(self.topic)
            return
        self.segments.hide()
        self.start.hide()
        hits = {id(question) for _, question in self.questions if found(question.answer, query)}
        for number, heading in enumerate(self.headings):
            heading.setVisible(any(id(q) in hits for topic, q in self.questions if topic == number))
        for _, question in self.questions:
            question.setVisible(id(question) in hits)
            question.show_piece(snippet(question.answer, query) if id(question) in hits else "")
        self.lead.setText(
            tr("Answers for “{query}”: {n}", query=query, n=len(hits))
            if hits
            else tr("Nothing found for “{query}”. Try another word or clear the search to see the topics.", query=query)
        )
