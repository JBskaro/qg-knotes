# SPDX-License-Identifier: GPL-3.0-only
"""Projects on the left of the meetings window, like folders in email: a click shows the project's meetings, a meeting
dragged onto a project moves into it. The project menu's "Color", "Icon" and "Default minutes type" submenus."""

import pathlib
from collections.abc import Callable

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QDragEnterEvent, QDragMoveEvent, QDropEvent, QMouseEvent, QPalette
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QMenu

from knotes import catalog, kinds
from knotes import projects as project_files  # the panel's own name for the project list is projects
from knotes.ui import style
from knotes.ui.meetings_list import MEETINGS_MIME
from knotes.language import tr

ALL = "\x00все"  # the "All projects" panel row: neither a project nor "no project"


class ProjectsPanel(QListWidget):
    """Projects on the left: a click shows the project's meetings, a meeting dragged onto a project moves into it."""

    selected = Signal(object)  # None - all meetings, "" - no project, otherwise the project name
    assign = Signal(list, str)  # meeting folders, project
    menu = Signal(str, QPoint)  # project and where to show its menu

    def __init__(self):
        super().__init__()
        self.setObjectName("разделы")
        self.setMinimumWidth(150)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # a long name is shortened
        self.setAcceptDrops(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.currentItemChanged.connect(self._current_changed)
        self.itemClicked.connect(self._clicked_again)
        self._changed_by_press = False  # the press of this click chose another row: it was announced already
        self._under_mouse: str | None = None  # project the dragged meeting is over now
        self._snapshot: tuple | None = None
        self._compact = False  # column collapsed to a strip: icons only, name and count in the tooltip
        self._shown: tuple | None = None  # what is shown: to redraw on collapsing

    def refresh_view(self) -> None:
        """The theme changed: redraw the icons on the next display."""
        self._snapshot = None

    def compact(self, yes: bool) -> None:
        self._compact = yes
        self.setMinimumWidth(0 if yes else 150)
        if self._shown is not None:
            self._snapshot = None
            self.display(*self._shown)

    @staticmethod
    def _key(element: QListWidgetItem) -> str | None:
        key = element.data(Qt.ItemDataRole.UserRole)
        return None if key == ALL else key

    def _current_changed(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current:
            self._changed_by_press = True
            self.selected.emit(self._key(current))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._changed_by_press = False
        super().mousePressEvent(event)

    def _clicked_again(self, element: QListWidgetItem) -> None:
        """A click on the row already chosen counts too: the project page comes back over an open meeting."""
        if not self._changed_by_press:
            self.selected.emit(self._key(element))
        self._changed_by_press = False

    def display(
        self, account: dict[str, int], projects: list[str], current: str | None, kind: dict[str, dict] | None = None
    ) -> None:
        """Rows "All projects", projects in alphabetical order and "No project", each with its meeting count.
        The list refreshes every 5 seconds: without changes the panel is not rebuilt, otherwise the row under the
        dragged meeting would disappear."""
        kind = kind or {}
        self._shown = (account, projects, current, kind)
        snapshot = (tuple(sorted(account.items())), tuple(projects), current, repr(sorted(kind.items())), self._compact)
        if snapshot == self._snapshot:
            return
        self._snapshot = snapshot
        color = self.palette().color(QPalette.ColorRole.WindowText)
        lines = [
            (tr("All projects"), ALL, sum(account.values()), style.icon(style.ALL_PROJECTS, color)),
            *((p, p, account.get(p, 0), style.project_icon(kind.get(p, {}), color)) for p in projects),
            (tr(catalog.NO_PROJECT), "", account.get("", 0), style.icon(style.FOLDER, color)),
        ]
        self.blockSignals(True)
        self.clear()
        for row_label, code, count, icon in lines:
            element = QListWidgetItem(icon, "" if self._compact else f"{row_label}  ({count})", self)
            element.setToolTip(f"{row_label}  ({count})" if self._compact else "")
            element.setData(Qt.ItemDataRole.UserRole, code)
            if code == (ALL if current is None else current):
                self.setCurrentItem(element)
        self.blockSignals(False)
        self._light_up(self._under_mouse)

    def _menu(self, point: QPoint) -> None:
        element = self.itemAt(point)
        self.menu.emit(self._key(element) or "" if element else "", self.viewport().mapToGlobal(point))

    def _target(self, event: QDragMoveEvent) -> QListWidgetItem | None:
        element = self.itemAt(event.position().toPoint())
        if not event.mimeData().hasFormat(MEETINGS_MIME) or element is None or self._key(element) is None:
            return None
        return element

    def _light_up(self, project: str | None) -> None:
        """Highlight the project the meeting is about to be dropped on (None - remove the highlight)."""
        self._under_mouse = project
        color = style.accent()
        color.setAlpha(60)
        for number in range(self.count()):
            element = self.item(number)
            above = project is not None and element.data(Qt.ItemDataRole.UserRole) == project
            element.setBackground(color if above else QBrush())

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasFormat(MEETINGS_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        target = self._target(event)
        self._light_up(None if target is None else self._key(target))
        if target is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragMoveEvent) -> None:
        self._light_up(None)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        target = self._target(event)
        project = None if target is None else self._key(target)
        self._light_up(None)
        if project is None:
            return
        folders = [p for p in bytes(event.mimeData().data(MEETINGS_MIME)).decode().split("\n") if p]
        event.acceptProposedAction()
        if folders:
            self.assign.emit(folders, project)


def minutes_menu(menu: QMenu, project: str, recordings: pathlib.Path) -> None:
    """The "Default minutes type" submenu: the project's meeting kind, its sections are used for the minutes."""
    selected = kinds.kind(project_files.kind(recordings, project)).name
    submenu = menu.addMenu(tr("Default minutes type"))
    for kind_ in kinds.kinds():
        item = submenu.addAction(
            tr(kind_.name),
            lambda i=kind_.name: project_files.set_kind(recordings, project, "" if i == kinds.WORKING else i),
        )
        item.setCheckable(True)
        item.setChecked(kind_.name == selected)
        item.setToolTip(tr(kind_.description))
    submenu.setToolTipsVisible(True)


def looks_menu(menu: QMenu, kind: dict, text: QColor, chosen: Callable[..., None]) -> None:
    """The project's "Color" and "Icon" submenus: a color sample and the icon itself right in the item; chosen gets
    color= or icon=."""
    colors = menu.addMenu(tr("Color"))
    colors.addAction(tr("No color"), lambda: chosen(color=None)).setCheckable(True)
    for number, name in enumerate(style.PROJECT_COLORS):
        colors.addAction(
            style.icon(style.DOT, style.participant_color(number)[0]),
            tr(name),
            lambda n=number: chosen(color=n),
        ).setCheckable(True)
    colors.actions()[(kind.get("цвет") if kind.get("цвет") is not None else -1) + 1].setChecked(True)
    icons = menu.addMenu(tr("Icon"))
    for sign, (_, name) in style.PROJECT_ICONS.items():
        item = icons.addAction(
            style.project_icon(kind | {"значок": sign}, text),
            tr(name),
            lambda k=sign: chosen(icon=k),
        )
        item.setCheckable(True)
        item.setChecked(sign == kind.get("значок", "папка"))
