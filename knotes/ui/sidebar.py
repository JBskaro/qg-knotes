# SPDX-License-Identifier: GPL-3.0-only
"""Left column of the meetings window, like the navigation pane in Windows apps: the "Meetings", "Tasks" and "Summary"
sections on top, the projects below them, "Settings", "Help" and "Collapse" at the bottom. A section opens on the
right while the column stays in place; the collapsed column is a strip of icons with a tooltip on hover."""

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from knotes.ui import style
from knotes.language import tr_key, tr

SECTIONS = (
    ("встречи", style.VIDEO, tr_key("Встречи")),
    ("задачи", style.TASKS, tr_key("Задачи")),
    ("сводка", style.CHART, tr_key("Сводка")),
)
BOTTOM = (
    ("настройки", style.SETTINGS, tr_key("Настройки")),
    ("справка", style.HELP, tr_key("Справка")),
    ("свернуть", style.COLLAPSE_PANEL, tr_key("Свернуть")),
)
STRIP_WIDTH = 48
COLUMN_WIDTH = 150  # the expanded column cannot be narrower


class _SectionList(QListWidget):
    """A few items without scrolling: the height fits the rows exactly."""

    def __init__(self):
        super().__init__()
        self.setObjectName("разделы")
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

    def sizeHint(self) -> QSize:
        height = sum(self.sizeHintForRow(number) for number in range(self.count())) + 2 * self.frameWidth()
        return QSize(super().sizeHint().width(), height + 4)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.sizeHint().height())


class SidebarPanel(QFrame):
    """Column of sections around the projects panel. Signal selected(section): a click on a section or "Collapse";
    the highlight changes only when the section is opened (select), not on the click itself."""

    selected = Signal(str)  # встречи, задачи, сводка, настройки, справка or свернуть
    new_project = Signal()

    def __init__(self, projects: QListWidget):
        super().__init__()
        self.section, self.collapsed, self.projects = "встречи", False, projects
        self._counts: dict[str, int] = {}
        self._top, self._bottom = _SectionList(), _SectionList()
        for list_, items in ((self._top, SECTIONS), (self._bottom, BOTTOM)):
            for code, _, _ in items:
                QListWidgetItem(list_).setData(Qt.ItemDataRole.UserRole, code)
            list_.itemClicked.connect(self._clicked_item)
        self._row_label = QLabel(tr("Projects"))
        self._row_label.setObjectName("второй_текст")
        self._plus = style.icon_button(style.PLUS, tr("New project"), self.new_project.emit)
        header = QHBoxLayout()
        header.setContentsMargins(10, 8, 0, 0)
        header.addWidget(self._row_label)
        header.addStretch()
        header.addWidget(self._plus)
        column = QVBoxLayout(self)
        column.setContentsMargins(6, 6, 6, 6)
        column.setSpacing(4)
        for part in (self._top, header, projects, self._bottom):
            if isinstance(part, QHBoxLayout):
                column.addLayout(part)
            else:
                column.addWidget(part, 1 if part is projects else 0)
        self.setMinimumWidth(COLUMN_WIDTH)
        self.repaint_theme()
        self.select("встречи")

    def element(self, code: str) -> QListWidgetItem:
        return next(
            list_.item(n)
            for list_ in (self._top, self._bottom)
            for n in range(list_.count())
            if list_.item(n).data(Qt.ItemDataRole.UserRole) == code
        )

    def place(self, code: str) -> tuple[QWidget, QRect]:
        """Where a section's row is: the list viewport and the row's rectangle in it (for first-run tips)."""
        element = self.element(code)
        list_ = element.listWidget()
        return list_.viewport(), list_.visualItemRect(element)

    def _clicked_item(self, element: QListWidgetItem) -> None:
        self.selected.emit(element.data(Qt.ItemDataRole.UserRole))
        self.select(self.section)  # the section did not open or it is "Collapse": keep the highlight

    def select(self, section: str) -> None:
        """Highlight the open section; a page without its own section has no highlight."""
        self.section = section
        for list_ in (self._top, self._bottom):
            list_.blockSignals(True)
            list_.clearSelection()
            list_.blockSignals(False)
        if section in {k for k, *_ in (*SECTIONS, *BOTTOM)}:
            self.element(section).setSelected(True)

    def set_counts(self, **account: int) -> None:
        """Number of meetings and open tasks next to the sections: "Meetings (7)"."""
        self._counts.update(account)
        self._label_items()

    def _label_items(self) -> None:
        for code, _, row_label in (*SECTIONS, *BOTTOM):
            if code == "свернуть" and self.collapsed:
                row_label = tr_key("Развернуть")
            text = tr(row_label) + (f"  ({self._counts[code]})" if code in self._counts else "")
            element = self.element(code)
            element.setText("" if self.collapsed else text)
            element.setToolTip(text if self.collapsed else "")

    def repaint_theme(self) -> None:
        """Icons in the theme's text color: redrawn after a theme change."""
        color = self.palette().color(QPalette.ColorRole.WindowText)
        for code, sign, _ in (*SECTIONS, *BOTTOM):
            if code == "свернуть" and self.collapsed:
                sign = style.EXPAND_PANEL
            self.element(code).setIcon(style.icon(sign, color))
        self._label_items()

    def collapse(self, yes: bool) -> None:
        """A strip of icons 48 wide or a column with labels; the column never disappears completely."""
        if yes == self.collapsed:
            return
        self.collapsed = yes
        for part in (self._row_label, self._plus):
            part.setVisible(not yes)
        if hasattr(self.projects, "compact"):
            self.projects.compact(yes)
        if yes:
            self.setFixedWidth(STRIP_WIDTH)
        else:
            self.setMinimumWidth(COLUMN_WIDTH)
            self.setMaximumWidth(16_777_215)  # QWIDGETSIZE_MAX: the width can be dragged again
        self.repaint_theme()
