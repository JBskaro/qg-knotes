# SPDX-License-Identifier: GPL-3.0-only
"""Vocabulary window: how a word is spelled correctly and how speech recognition may hear it. However the window is
closed ("Done", the cross, Esc), the words are kept, a word being typed too."""

from PySide6.QtCore import QModelIndex
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from knotes import vocabulary
from knotes.ui import style
from knotes.language import tr


class VocabularyDialog(QDialog):
    def __init__(self, words: list[vocabulary.Word], parent_: QWidget | None = None):
        super().__init__(parent_)
        self.setWindowTitle(tr("Vocabulary - {name}", name=style.NAME))
        self.resize(620, 420)
        hint = QLabel(
            tr(
                "Speech recognition confuses rare words, names and abbreviations. On the left, the correct spelling; "
                "on the right, how recognition may hear it, separated by commas. The transcript is fixed "
                'automatically, keeping word endings: "cloud" becomes "Claude".'
            )
        )
        hint.setWordWrap(True)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([tr("Correct spelling"), tr("May be heard as")])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 180)
        self.table.verticalHeader().setVisible(False)
        for word in words:
            self._add(word.correct, ", ".join(word.variants))
        add = QPushButton(tr("Add word"))
        delete, done = QPushButton(tr("Delete")), QPushButton(tr("Done"))
        add.clicked.connect(self._new_value)
        delete.clicked.connect(lambda: self.table.removeRow(self.table.currentRow()))
        done.clicked.connect(self.accept)
        row = QHBoxLayout()
        for button in (add, delete):
            row.addWidget(button)
        row.addStretch()
        row.addWidget(done)
        column = QVBoxLayout(self)
        column.addWidget(hint)
        column.addWidget(self.table, 1)
        column.addLayout(row)

    def done(self, _result: int) -> None:
        """Any close keeps the words: the cell being edited is written first."""
        self.table.setCurrentIndex(QModelIndex())
        super().done(QDialog.DialogCode.Accepted)

    def _add(self, correct: str, variants: str) -> None:
        line = self.table.rowCount()
        self.table.insertRow(line)
        self.table.setItem(line, 0, QTableWidgetItem(correct))
        self.table.setItem(line, 1, QTableWidgetItem(variants))

    def _new_value(self) -> None:
        self._add("", "")
        line = self.table.rowCount() - 1
        self.table.setCurrentCell(line, 0)
        self.table.editItem(self.table.item(line, 0))

    def words(self) -> list[vocabulary.Word]:
        result = []
        for line in range(self.table.rowCount()):
            correct, variants = (self.table.item(line, k).text() if self.table.item(line, k) else "" for k in (0, 1))
            if correct.strip():
                result.append(
                    vocabulary.Word(correct.strip(), tuple(v.strip() for v in variants.split(",") if v.strip()))
                )
        return result
