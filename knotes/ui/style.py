# SPDX-License-Identifier: GPL-3.0-only
"""Shared look: the QG Knotes name and icon, window colors for the light and dark theme, the switch and segments,
icons from the Windows font and a translucent toast at the top of the screen (like the OSD in AIMP)."""

import html
import re
from collections.abc import Callable, Sequence

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QPointF, QPropertyAnimation, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QActionGroup,
    QColor,
    QCursor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QIcon,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPalette,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from knotes import catalog, system
from knotes.language import tr_key, tr

_NAME = tr_key("Узелок")


def __getattr__(name: str) -> str:
    """NAME is the program name in the interface language: the language is set at startup, after the module loads."""
    if name == "NAME":
        return tr(_NAME)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Glyphs of the Segoe Fluent Icons (Windows 11) / Segoe MDL2 Assets (Windows 10) font, the codes are the same
MICROPHONE, HEADPHONES, DOT = "", "", ""
COLLAPSE, EXPAND, UP, DOWN = "", "", "", ""
RIGHT = ""
STAR, PAUSE, LATER_SPAN, STOP = "", "", "", ""
SEARCH, SETTINGS, HELP, MORE, REFRESH, EYE = "", "", "", "", "", ""
FILTER_FILES, FOLDER, DOCUMENT, SEND, UPLOAD = "", "", "", "", ""
PLUS, CLOSE, BOOK, VIDEO = "", "", "", ""
CHART = ""
ALL_PROJECTS, COLLAPSE_PANEL, EXPAND_PANEL = "\ue8f1", "\ue89f", "\ue8a0"  # sections column

RED = QColor("#E24B4A")
ACCENT, ACCENT_DARK = QColor("#0F6E68"), QColor("#1A8A82")  # teal; lighter in the dark theme, white text stays readable
DARKER = "#0A524D"  # button under the pointer
TRANSPARENCY, UNDER_MOUSE = 205, 240  # toast background opacity: normal and under the pointer (of 255)
TOP_MARGIN = 10
ICON_SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
NOTE, TASKS = "", ""  # "Edit" (pencil) and "Task list" of the Windows icon font
TOAST_FLAGS = (
    Qt.WindowType.Tool
    | Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.WindowDoesNotAcceptFocus
)

# Calm participant colors: (text, background) in the light theme and in the dark one
PASTEL = [
    (("#3C3489", "#EEEDFE"), ("#CECBF6", "rgba(127,119,221,0.24)")),
    (("#712B13", "#FAECE7"), ("#F5C4B3", "rgba(216,90,48,0.22)")),
    (("#085041", "#E1F5EE"), ("#9FE1CB", "rgba(29,158,117,0.22)")),
    (("#72243E", "#FBEAF0"), ("#F4C0D1", "rgba(212,83,126,0.22)")),
    (("#633806", "#FAEEDA"), ("#FAC775", "rgba(186,117,23,0.24)")),
    (("#0C447C", "#E6F1FB"), ("#B5D4F4", "rgba(55,138,221,0.24)")),
    (("#27500A", "#EAF3DE"), ("#C0DD97", "rgba(99,153,34,0.24)")),
]
# Meeting state labels: (text, background) in the light and in the dark theme
STATES = {
    "готова": (("#27500A", "#EAF3DE"), ("#C0DD97", "rgba(99,153,34,0.22)")),
    "идет": (("#0C447C", "#E6F1FB"), ("#B5D4F4", "rgba(55,138,221,0.24)")),
    "ошибка": (("#791F1F", "#FCEBEB"), ("#F7C1C1", "rgba(226,75,74,0.22)")),
    "ждет": (("#5F5E5A", "#F1EFE8"), ("#D3D1C7", "rgba(255,255,255,0.08)")),
}


def dark() -> bool:
    return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark


def participant_color(number: int) -> tuple[str, str]:
    """(text, background) of a participant by their number in the meeting, for the current theme."""
    return PASTEL[number % len(PASTEL)][dark()]


# Color and icon names are translated when shown: tr(name)
PROJECT_COLORS = (
    tr_key("Сиреневый"),
    tr_key("Коралловый"),
    tr_key("Бирюзовый"),
    tr_key("Розовый"),
    tr_key("Янтарный"),
    tr_key("Синий"),
    tr_key("Зеленый"),
)  # as PASTEL
PROJECT_ICONS = {
    "папка": ("", tr_key("Папка")),
    "портфель": ("", tr_key("Портфель")),
    "люди": ("", tr_key("Люди")),
    "звезда": ("", tr_key("Звезда")),
    "флаг": ("", tr_key("Флаг")),
    "дом": ("", tr_key("Дом")),
    "коробка": ("", tr_key("Коробка")),
    "корзина": ("", tr_key("Корзина")),
    "деньги": ("", tr_key("Деньги")),
    "график": ("", tr_key("График")),
    "шестеренка": ("", tr_key("Шестеренка")),
    "лампочка": ("", tr_key("Лампочка")),
}


def project_chip(color: int | None) -> str:
    """Project label in a meeting row: the project color, or a calm gray without a color."""
    if color is None:
        return chip("ждет")
    text, background_ = participant_color(color)
    return f"color: {text}; background: {background_}; border-radius: 10px; padding: 2px 10px; font-size: 12px;"


def project_icon(kind: dict, text_color: QColor) -> QIcon:
    """Project icon in the left panel: the chosen one (a folder by default) in the project color."""
    sign = PROJECT_ICONS.get(kind.get("значок", ""), PROJECT_ICONS["папка"])[0]
    return icon(sign, participant_color(kind["цвет"])[0] if kind.get("цвет") is not None else text_color)


def state_color(kind: str) -> tuple[str, str]:
    return STATES.get(kind, STATES["ждет"])[dark()]


def accent() -> QColor:
    return ACCENT_DARK if dark() else ACCENT


def apply_accent(app: QApplication) -> None:
    """One accent color in all windows: check marks, selection, links (otherwise Qt takes the color from Windows)."""
    palette_ = app.palette()
    for role in (QPalette.ColorRole.Accent, QPalette.ColorRole.Highlight, QPalette.ColorRole.Link):
        palette_.setColor(role, accent())
    palette_.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    app.setPalette(palette_)


def icon_font(size: int) -> QFont:
    family = "Segoe Fluent Icons" if "Segoe Fluent Icons" in QFontDatabase.families() else "Segoe MDL2 Assets"
    font = QFont(family)
    font.setPixelSize(size)
    return font


def _pixmap(sign: str, color: QColor, size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setFont(icon_font(max(8, int(size * 0.8))))
    painter.setPen(color)
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, sign)
    painter.end()
    return pixmap


def icon(sign: str, color: QColor | str = "#FFFFFF") -> QIcon:
    """An icon from a font glyph in all the sizes Windows needs at different screen scales."""
    result = QIcon()
    for size in ICON_SIZES[:6]:
        result.addPixmap(_pixmap(sign, QColor(color), size))
    return result


# A wave tied in a knot (naming session sketch): start and Bezier curves in fractions of the icon side
KNOT_WAVE = [
    ((0.15, 0.43),),
    ((0.19, 0.43), (0.21, 0.62), (0.28, 0.62)),
    ((0.34, 0.62), (0.40, 0.46), (0.50, 0.36)),
    ((0.59, 0.28), (0.63, 0.16), (0.52, 0.16)),
    ((0.41, 0.16), (0.36, 0.36), (0.45, 0.54)),
    ((0.48, 0.62), (0.55, 0.62), (0.59, 0.52)),
    ((0.61, 0.43), (0.64, 0.38), (0.68, 0.42)),
    ((0.72, 0.46), (0.71, 0.60), (0.77, 0.60)),
    ((0.81, 0.60), (0.84, 0.52), (0.86, 0.48)),
]


def _knot_pixmap(size: int, recording_now: bool) -> QPixmap:
    """A teal tile with a white wave tied in a knot; a red dot in the corner while recording."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    x_ = QPainter(pixmap)
    x_.setRenderHint(QPainter.RenderHint.Antialiasing)
    x_.setPen(Qt.PenStyle.NoPen)
    x_.setBrush(ACCENT)
    x_.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
    thickness = 0.11 if size <= 24 else 0.085  # thicker when small, or the line gets lost
    path = QPainterPath(QPointF(KNOT_WAVE[0][0][0] * size, KNOT_WAVE[0][0][1] * size))
    for a, b, v in KNOT_WAVE[1:]:
        path.cubicTo(*(d * size for point in (a, b, v) for d in point))
    x_.setPen(
        QPen(
            QColor("#FFFFFF"),
            max(1.5, thickness * size),
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
    )
    x_.setBrush(Qt.BrushStyle.NoBrush)
    x_.drawPath(path)
    if recording_now:
        s = size * 0.40
        x_.setPen(QPen(QColor("#FFFFFF"), max(1.0, 0.06 * size)))
        x_.setBrush(RED)
        x_.drawEllipse(QRectF(size - s, size - s, s - 0.5, s - 0.5))
    x_.end()
    return pixmap


def tray_icon(recording_now: bool) -> QIcon:
    """The program icon: next to the clock, in window titles and on the exe; with a red dot while recording."""
    result = QIcon()
    for size in ICON_SIZES:
        result.addPixmap(_knot_pixmap(size, recording_now))
    return result


class Switch(QAbstractButton):
    """An "on/off" switch as in Windows 11 Settings."""

    def __init__(self, enabled: bool = False, parent_: QWidget | None = None):
        super().__init__(parent_)
        self.setCheckable(True)
        self.setChecked(enabled)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:
        return QSize(40, 20)

    def paintEvent(self, _event_of: QPaintEvent) -> None:
        x_ = QPainter(self)
        x_.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(1, 1, 38, 18)
        text = self.palette().color(QPalette.ColorRole.WindowText)
        if self.isChecked():
            x_.setPen(Qt.PenStyle.NoPen)
            x_.setBrush(accent())
        else:
            x_.setPen(QPen(text, 1))
            x_.setBrush(Qt.BrushStyle.NoBrush)
        x_.drawRoundedRect(track, 9, 9)
        x_.setPen(Qt.PenStyle.NoPen)
        x_.setBrush(QColor("#FFFFFF") if self.isChecked() else text)
        x_.drawEllipse(
            QPointF(30 if self.isChecked() else 10, 10), 6 if self.underMouse() else 5, 6 if self.underMouse() else 5
        )
        x_.end()


class Segments(QWidget):
    """A choice of one of several options side by side; signal selected(value)."""

    selected = Signal(object)

    def __init__(self, variants: Sequence[tuple[object, str]], current: object):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self._group = QButtonGroup(self)
        self._buttons: dict[object, QPushButton] = {}
        for number, (value, row_label) in enumerate(variants):
            button = QPushButton(row_label)
            button.setCheckable(True)
            button.setAutoDefault(False)  # in a dialog, Enter in a field does not pick an option
            button.setChecked(value == current)
            button.setObjectName(
                "сегмент_первый" if number == 0 else "сегмент_последний" if number == len(variants) - 1 else "сегмент"
            )
            button.clicked.connect(lambda _pressed=False, z=value: self.selected.emit(z))
            self._group.addButton(button)
            self._buttons[value] = button
            row.addWidget(button)
        row.addStretch()

    def select(self, value: object) -> None:
        """Selects an option from code; the selected signal is not emitted."""
        self._buttons[value].setChecked(True)


def icon_button(
    sign: str, tooltip: str, action: Callable[[], object] | None = None, parent_: QWidget | None = None
) -> QToolButton:
    """A flat button with an icon from the Windows font and a tooltip."""
    button = QToolButton(parent_)
    button.setObjectName("значок")
    button.setText(sign)
    button.setToolTip(tooltip)
    if action:
        button.clicked.connect(action)
    return button


HIGHLIGHT = {False: ("#FAEEDA", "#FAC775"), True: ("#633806", "#854F0B")}  # (found, current) in light and dark


def highlighted(text: str, pattern: re.Pattern, color: Callable[[int], str]) -> tuple[str, int]:
    """Outside text as markup with the matches of pattern (one group) on a background, color(number of the match);
    and how many matches there are."""
    parts = pattern.split(text)
    result = []
    for number, part in enumerate(parts):
        if number % 2:
            result.append(f"<span style='background:{color(number // 2)}'>{html.escape(part)}</span>")
        else:
            result.append(html.escape(part))
    return "".join(result), len(parts) // 2


def menu_text(text: str) -> str:
    """Outside text (a project, a label, a name) for a button or a menu item: Qt takes "&" there for a shortcut
    mark and would show "R&D" as "RD"."""
    return text.replace("&", "&&")


def restyle(widget: QWidget, name: str | None = None) -> None:
    """Applies the style again after a style property changed; name: the new style name (objectName) first."""
    if name is not None:
        if widget.objectName() == name:
            return
        widget.setObjectName(name)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def ask(parent_: QWidget, title: str, text: str, warning: bool = False) -> bool:
    """A message box with outside text (a task, a label name, a server answer) as plain text: as markup, a link in
    it would be live. A question has Yes and No and gives True for Yes, a warning has OK."""
    box = QMessageBox(
        QMessageBox.Icon.Warning if warning else QMessageBox.Icon.Question,
        title,
        text,
        QMessageBox.StandardButton.Ok if warning else QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        parent_,
    )
    box.setTextFormat(Qt.TextFormat.PlainText)
    return box.exec() in (QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Ok)


def projects_menu(
    parent_: QWidget,
    projects: Sequence[str],
    project_looks: dict[str, dict],
    current: str | None,
    select: Callable[[str | None], object],
) -> QMenu:
    """Project filter menu: "All projects" (None), projects with their icons, "No project" (""); current is checked."""
    menu, group = QMenu(parent_), QActionGroup(parent_)
    color = parent_.palette().color(QPalette.ColorRole.WindowText)
    for row_label, project, sign in (
        (tr("All projects"), None, icon(VIDEO, color)),
        *((p, p, project_icon(project_looks.get(p, {}), color)) for p in projects),
        (tr(catalog.NO_PROJECT), "", icon(FOLDER, color)),
    ):
        item = menu.addAction(sign, menu_text(row_label), lambda p=project: select(p))
        item.setCheckable(True)
        item.setChecked(project == current)
        group.addAction(item)
    return menu


def more_button(items: Sequence[tuple[str, Callable[[], object]]]) -> QToolButton:
    """A "…" button with a menu of less frequent actions."""
    button = icon_button(MORE, tr("More"))
    menu = QMenu(button)
    for row_label, action in items:
        menu.addAction(row_label, action)
    button.setMenu(menu)
    button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    return button


def plain_tooltip(text: str) -> str:
    """A tooltip with outside text (a calendar title, a server answer): Qt would take tags in it as markup and, for
    example, load an image from a link. The "<qt>" wrapper with escaping shows the text as is."""
    return f"<qt>{html.escape(text).replace(chr(10), '<br>')}</qt>" if text else ""


class ClearableLine(QLineEdit):
    """A search or add line: Esc clears it, and an empty one passes Esc on (to close a panel)."""

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and self.text():
            self.clear()
        else:
            super().keyPressEvent(event)


class Elided(QLabel):
    """A one-line label no wider than its place: what does not fit ends with "…", the full text is in the tooltip."""

    def __init__(self, text: str, tooltip: str = ""):
        super().__init__()
        self._full, self._toast = text, tooltip
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setMinimumWidth(36)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)  # full width while space allows
        self.setText(text)
        self.setToolTip(plain_tooltip(tooltip))

    def full(self) -> str:
        return self._full

    def sizeHint(self) -> QSize:
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 4, super().sizeHint().height())

    def resizeEvent(self, event: QResizeEvent) -> None:
        visible = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, self.width())
        self.setText(visible)
        shortened = visible != self._full
        self.setToolTip(plain_tooltip("\n".join(ch for ch in (self._full if shortened else "", self._toast) if ch)))
        super().resizeEvent(event)


class WrappedCaption(QLabel):
    """A wrapped label of a set width whose height follows its text, also where a layout centers it: the layout would
    take the height for the whole row width, one line, and then narrow the label, cutting the other lines."""

    def __init__(self, text: str, width: int, rich: bool = False):
        super().__init__(text)
        self.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setFixedWidth(width)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter)

    def hasHeightForWidth(self) -> bool:
        return False  # the height comes from sizeHint at the label's own width

    def sizeHint(self) -> QSize:
        return QSize(self.width(), self.heightForWidth(self.width()))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()


def caption(text: str) -> QLabel:
    """A label with outside text (a title, a name, a file name) as plain text: markup in it does not work."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def layer(*widgets: QWidget, padding: int = 8) -> QFrame:
    """The work area as a card slightly lighter than the window background, with a thin border (Windows 11 layers):
    columns and sections read apart. Side columns lie right on the window background."""
    card = QFrame()
    card.setObjectName("слой")
    column = QVBoxLayout(card)
    column.setContentsMargins(padding, padding, padding, padding)
    column.setSpacing(4)
    for widget in widgets:
        column.addWidget(widget)
    return card


def text_tones() -> tuple[str, str]:
    """(secondary, quiet) text colors of the current theme: hints and service text are quieter than the main text."""
    return ("#B4B2A9", "#888780") if dark() else ("#5F5E5A", "#888780")


def window_style() -> str:
    """Style of the program's own window parts (labels, list rows, segments) for the current theme."""
    dark_mode = dark()
    secondary, quiet = text_tones()
    frame_box = "rgba(255,255,255,0.16)" if dark_mode else "rgba(0,0,0,0.14)"
    base_name, layer_, edge = (
        ("#202020", "#2A2A2A", "rgba(255,255,255,0.07)") if dark_mode else ("#F3F3F3", "#FBFBFB", "rgba(0,0,0,0.07)")
    )
    hover, choice = (
        ("rgba(255,255,255,0.06)", "rgba(26,138,130,0.26)") if dark_mode else ("rgba(0,0,0,0.04)", "#E1F5EE")
    )
    choice_text, a = ("#9FE1CB", accent().name()) if dark_mode else ("#085041", accent().name())
    return f"""
QWidget#основа {{ background: {base_name}; }}
QFrame#слой {{ background: {layer_}; border: 1px solid {edge}; border-radius: 8px; }}
QFrame#слой QAbstractScrollArea, QFrame#слой QStackedWidget {{ background: transparent; border: none; }}
QFrame#слой QWidget#qt_scrollarea_viewport > QWidget {{ background: transparent; }}
QFrame#слой QPlainTextEdit#поле_записи {{ background: {"rgba(255,255,255,0.04)" if dark_mode else "#FFFFFF"};
    border: 1px solid {frame_box}; border-radius: 6px; padding: 4px; }}
QFrame#слой QPlainTextEdit#поле_записи:focus {{ border: 1px solid {a}; }}
QSplitter#колонки::handle {{ background: transparent; }}
QLabel#второй_текст {{ color: {secondary}; font-size: 12px; }}
QLabel#тихий_текст {{ color: {quiet}; font-size: 12px; }}
QLabel#группа {{ color: {secondary}; font-size: 12px; padding: 10px 2px 2px 2px; }}
QLabel#заголовок_встречи {{ font-weight: 600; }}
QLabel#заголовок_страницы {{ font-size: 22px; font-weight: 600; }}
QLabel#тема {{ font-size: 15px; font-weight: 600; padding: 14px 0 4px 10px; }}
QLabel#ответ {{ font-size: 14px; }}
QPushButton#вопрос {{ text-align: left; border: none; border-radius: 6px; padding: 9px 10px; font-size: 14px;
    background: transparent; }}
QPushButton#вопрос:hover {{ background: {hover}; }}
QLabel#успех {{ color: {"#97C459" if dark_mode else "#3B6D11"}; font-size: 12px; }}
QLabel#ошибка_текст {{ color: {"#F09595" if dark_mode else "#A32D2D"}; font-size: 12px; }}
QFrame#бросить {{ border: 1px dashed {frame_box}; border-radius: 6px; }}
QFrame#метка {{ border: none; border-top: 1px dashed {frame_box}; border-bottom: 1px dashed {frame_box}; }}
QFrame#разделитель {{ border: none; border-left: 1px solid {frame_box}; }}
QListWidget#встречи, QListWidget#разделы {{ border: none; background: transparent; outline: none; }}
QListWidget#встречи::item {{ border-bottom: 1px solid {frame_box}; }}
QListWidget#встречи::item:hover {{ background: {hover}; }}
QListWidget#встречи::item:selected {{ background: {choice}; }}
QListWidget#разделы::item {{ padding: 2px 10px; border-radius: 6px; margin: 1px 0; }}
QListWidget#разделы::item:hover {{ background: {hover}; }}
QListWidget#разделы::item:selected {{ background: {choice}; color: {choice_text}; }}
QToolButton#значок {{ font-family: "Segoe Fluent Icons", "Segoe MDL2 Assets"; font-size: 15px; border: none;
                      border-radius: 4px; padding: 6px; min-width: 20px; }}
QToolButton#значок:hover {{ background: {hover}; }}
QToolButton#значок:checked {{ background: {choice}; color: {choice_text}; }}
QToolButton#значок::menu-indicator {{ image: none; width: 0; }}
QToolButton#буквы {{ border: none; border-radius: 4px; padding: 5px 8px; font-size: 13px; }}
QToolButton#буквы:hover {{ background: {hover}; }}
QLineEdit#название_встречи {{ background: transparent; border: none; border-bottom: 1px solid transparent; }}
QLineEdit#название_встречи:hover, QLineEdit#название_встречи:focus {{ border-bottom: 1px solid {frame_box}; }}
QPushButton#главная {{ background: {a}; color: white; border: none; border-radius: 4px; padding: 7px 16px; }}
QPushButton#главная:hover {{ background: {DARKER}; }}
QPushButton#назад {{ border: none; background: transparent; color: {secondary}; padding: 2px 6px; }}
QPushButton#назад:hover {{ color: {a}; }}
QPushButton#стоп {{ background: #A32D2D; color: white; border: none; border-radius: 4px; padding: 7px 16px; }}
QPushButton#стоп:hover {{ background: #791F1F; }}
QPushButton#сегмент, QPushButton#сегмент_первый, QPushButton#сегмент_последний {{
    border: 1px solid {frame_box}; padding: 5px 12px; background: transparent; border-radius: 0; margin-left: -1px; }}
QPushButton#сегмент_первый {{ border-top-left-radius: 4px; border-bottom-left-radius: 4px; margin-left: 0; }}
QPushButton#сегмент_последний {{ border-top-right-radius: 4px; border-bottom-right-radius: 4px; }}
QPushButton#сегмент:checked, QPushButton#сегмент_первый:checked, QPushButton#сегмент_последний:checked {{
    background: {choice}; color: {choice_text}; }}
QPushButton#сегмент:hover, QPushButton#сегмент_первый:hover, QPushButton#сегмент_последний:hover {{
    background: {hover}; }}
QProgressBar#доля {{ border: none; border-radius: 2px; background: {frame_box}; max-height: 4px; }}
QProgressBar#ход {{ border: none; border-radius: 2px; background: {frame_box}; max-height: 3px; }}
QProgressBar#ход::chunk {{ background: {a}; border-radius: 2px; }}
"""


def chip(kind: str) -> str:
    """Style of a pill label for a meeting state, a key of STATES."""
    text, background_ = state_color(kind)
    return f"color: {text}; background: {background_}; border-radius: 10px; padding: 2px 10px; font-size: 12px;"


def participant_chip(number: int) -> str:
    text, background_ = participant_color(number)
    return f"color: {text}; background: {background_}; border-radius: 10px; padding: 2px 10px; font-weight: 600;"


HIDDEN_FROM_CAPTURE = "hidden_from_capture"  # Qt window property: Latin name, Qt does not find Cyrillic ones


class PopupHider(QObject):
    """The project list, menus and tooltips of the notepad and the recording bar are separate Windows windows: without
    this filter they would get into screen sharing and show the participants the projects and that recording is
    on. Animations of lists, menus and tooltips are off: Qt draws them in its own window that the filter would
    not hide in time."""

    def __init__(self, parent_: QObject | None = None):
        super().__init__(parent_)
        for effect in (
            Qt.UIEffect.UI_AnimateCombo,
            Qt.UIEffect.UI_AnimateMenu,
            Qt.UIEffect.UI_FadeMenu,
            Qt.UIEffect.UI_AnimateTooltip,
            Qt.UIEffect.UI_FadeTooltip,
        ):
            QApplication.setEffectEnabled(effect, False)

    def eventFilter(self, subject: QObject, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.Show
            and isinstance(subject, QWidget)
            and subject.windowType() in (Qt.WindowType.Popup, Qt.WindowType.ToolTip)
        ):
            under_mouse = QApplication.widgetAt(QCursor.pos())
            for owner in (QApplication.activeWindow(), under_mouse.window() if under_mouse else None):
                if owner is not None and owner is not subject and owner.property(HIDDEN_FROM_CAPTURE):
                    system.hide_from_capture(int(subject.winId()), True)
                    break
        return False


class Toast(QWidget):
    """A translucent dark toast over other windows: no frame, no focus, fades in.

    Under the mouse pointer it gets more opaque, to be easy to read and click.
    """

    def __init__(self, hide_from_capture: bool):
        super().__init__()
        self.setWindowFlags(TOAST_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet(TOAST_QSS)
        self._opacity = TRANSPARENCY
        self._fade_in = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_in.setDuration(180)
        self._fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.winId()  # the Windows window is created at once to set its properties
        self.hide_from_capture(hide_from_capture)

    def hide_from_capture(self, hide_: bool) -> None:
        """Its popups (lists, menus, tooltips) are hidden by PopupHider, by this property."""
        self.setProperty(HIDDEN_FROM_CAPTURE, hide_)
        system.hide_from_capture(int(self.winId()), hide_)

    def paintEvent(self, _event_of: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
        painter.fillPath(path, QColor(30, 30, 30, self._opacity))
        painter.setPen(QColor(255, 255, 255, 36))
        painter.drawPath(path)
        painter.end()

    def event(self, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.Enter, QEvent.Type.Leave):
            self._opacity = UNDER_MOUSE if event.type() == QEvent.Type.Enter else TRANSPARENCY
            self.update()
        return super().event(event)

    def appear(self) -> None:
        self.setWindowOpacity(0.0)
        self.show()
        self._fade_in.stop()
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)
        self._fade_in.start()

    def fit_size(self) -> None:
        """Size to the content, after the style is applied (fonts change the line height)."""
        self.ensurePolished()
        self.adjustSize()

    def top_center(self, below: int = 0) -> None:
        """At the top center of the primary screen; below: how far down from the top (under the recording bar)."""
        self.fit_size()
        screen_capture = QGuiApplication.primaryScreen().availableGeometry()
        self.move(screen_capture.center().x() - self.width() // 2, screen_capture.top() + TOP_MARGIN + below)


def empty(layout: QLayout, keep: tuple[QWidget, ...] = ()) -> None:
    """Takes everything out of the layout; its widgets, except keep, are hidden at once and deleted later, so they don't
    show through the new ones until then."""
    while (element := layout.takeAt(0)) is not None:
        widget = element.widget()
        if widget is not None and all(widget is not z for z in keep):
            widget.hide()
            widget.deleteLater()


class DraggedToast(Toast):
    """A toast window moved by dragging it anywhere; dropped() after a move, to remember the place."""

    def __init__(self, hide_from_capture: bool):
        super().__init__(hide_from_capture)
        self._shift: QPoint | None = None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._shift = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._shift is not None:
            self.move(event.globalPosition().toPoint() - self._shift)

    def mouseReleaseEvent(self, _event_of: QMouseEvent) -> None:
        if self._shift is not None:
            self._shift = None
            self.dropped()

    def dropped(self) -> None:
        """After a move by dragging."""


TOAST_QSS = f"""
QLabel {{ color: #F1EFE8; font-family: "Segoe UI Variable Text"; font-size: 14px; background: transparent; }}
QLabel#второй {{ color: #D3D1C7; font-size: 13px; }}
QLabel#время {{ font-size: 15px; font-weight: 600; }}
QLabel#предупреждение {{ color: #FAC775; font-size: 13px; }}
QLabel#значок {{ font-family: "Segoe Fluent Icons", "Segoe MDL2 Assets"; font-size: 14px; color: #B4B2A9; }}
QLabel#точка {{ font-family: "Segoe Fluent Icons", "Segoe MDL2 Assets"; font-size: 12px; color: #F09595; }}
QLabel#точка_пауза {{ font-family: "Segoe Fluent Icons", "Segoe MDL2 Assets"; font-size: 12px; color: #888780; }}
QPushButton {{ color: #F1EFE8; background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.16);
              border-radius: 6px; padding: 4px 12px; font-family: "Segoe UI Variable Text"; font-size: 13px; }}
QPushButton:hover {{ background: rgba(255,255,255,0.18); }}
QPushButton#главная {{ background: {ACCENT.name()}; border-color: {ACCENT.name()}; }}
QPushButton#главная:hover {{ background: {DARKER}; }}
QPushButton#стоп {{ background: #A32D2D; border-color: #A32D2D; }}
QPushButton#стоп:hover {{ background: #791F1F; }}
QPushButton#тихая {{ background: transparent; border: none; color: #D3D1C7; padding: 2px 6px; }}
QPushButton#тихая:hover {{ color: #FFFFFF; }}
QPushButton#значок {{ background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.16); color: #F1EFE8;
                     padding: 4px 7px; font-family: "Segoe Fluent Icons", "Segoe MDL2 Assets"; font-size: 14px; }}
QPushButton#значок:hover {{ color: #FFFFFF; background: rgba(255,255,255,0.18); }}
"""
