# SPDX-License-Identifier: GPL-3.0-only
"""The style sheet of the task board in the current theme: cards, columns, filters, the card panel and its calendar;
the style properties the sheet reacts to."""

from knotes.ui import style

# Style properties: the card is open in the panel, chosen with the keys, a filter is set. Latin names: Qt looks up
# the property from [property="true"] by its Latin-1 name
OPENED, CHOSEN, SET = "open", "current", "active"


def sheet() -> str:
    """The window style with the board's own rules."""
    dark = style.dark()
    background_, frame_box, hover = (
        ("#323232", "rgba(255,255,255,0.08)", "rgba(255,255,255,0.22)")
        if dark
        else ("#FFFFFF", "rgba(0,0,0,0.09)", "rgba(0,0,0,0.22)")
    )
    panel, edge = ("#2A2A2A", "rgba(255,255,255,0.07)") if dark else ("#FBFBFB", "rgba(0,0,0,0.07)")
    shadow, choice, choice_text = (
        ("rgba(255,255,255,0.06)", "rgba(26,138,130,0.26)", "#9FE1CB")
        if dark
        else ("rgba(0,0,0,0.04)", "#E1F5EE", "#085041")
    )
    secondary, quiet = style.text_tones()
    a = style.accent().name()
    k = "QFrame#слой QListWidget#колонка_задач "  # stronger than the general rule about transparency in a layer

    def cards(tail: str = "") -> str:
        return ", ".join(f"{k}QFrame#{name}{tail}" for name in ("карточка_задачи", "вопрос_доски"))

    return (
        style.window_style()
        + f"""
{cards()} {{ background: {background_}; border: 1px solid {frame_box}; border-radius: 8px; }}
{k}QFrame#вопрос_доски {{ border-style: dashed; }}
{cards(":hover")} {{ border-color: {hover}; }}
{cards(f'[{OPENED}="true"]')}, {cards(f'[{CHOSEN}="true"]')} {{ border-color: {a}; }}
QListWidget#колонка_задач {{ background: transparent; border: none; outline: none; }}
QListWidget#колонка_задач::item, QListWidget#колонка_задач::item:selected,
QListWidget#колонка_задач::item:hover {{ background: transparent; border: none; }}
{k}QFrame#черта {{ background: {a}; border: none; border-radius: 1px; }}
QLabel#сделано {{ color: {secondary}; }}
QLabel#скоро_текст {{ color: {"#F2B45A" if dark else "#9A5D00"}; font-size: 12px; }}
QLineEdit#быстрая {{ background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 5px 6px;
                     color: {secondary}; }}
QLineEdit#быстрая:hover {{ background: {shadow}; }}
QLineEdit#быстрая:focus {{ background: {background_}; border-color: {frame_box}; color: palette(text); }}
QPushButton#отбор {{ background: transparent; border: 1px solid {frame_box}; border-radius: 4px;
                     padding: 5px 24px 5px 10px; }}
QPushButton#отбор:checkable {{ padding: 5px 10px; }}
QPushButton#отбор:hover {{ background: {shadow}; }}
QPushButton#отбор[{SET}="true"], QPushButton#отбор:checked {{ background: {choice}; color: {choice_text};
                                                               border-color: transparent; }}
QPushButton#отбор::menu-indicator {{ subcontrol-origin: padding; subcontrol-position: right center; right: 8px; }}
QFrame#панель {{ background: {panel}; border: 1px solid {edge}; border-radius: 8px; }}
QFrame#панель QScrollArea, QWidget#содержимое_панели {{ background: transparent; border: none; }}
QTextEdit#заголовок_задачи {{ background: transparent; border: none; font-size: 16px; font-weight: 600; }}
QFrame#панель QTextEdit#описание {{ background: {"rgba(255,255,255,0.05)" if dark else "#FFFFFF"};
                                   border: 1px solid {frame_box}; border-radius: 4px; }}
QFrame#панель QTextEdit#описание:focus {{ border-bottom: 2px solid {a}; }}
QPushButton#опасная {{ color: {"#F09595" if dark else "#A32D2D"}; }}
QToolButton#крестик {{ font-family: "Segoe Fluent Icons", "Segoe MDL2 Assets"; font-size: 10px; border: none;
                       border-radius: 4px; padding: 4px; color: {quiet}; background: transparent; }}
QToolButton#крестик:hover {{ background: {shadow}; color: palette(text); }}
QCalendarWidget QWidget#qt_calendar_navigationbar {{ background: transparent; }}
QCalendarWidget QToolButton {{ color: palette(text); background: transparent; border: none; border-radius: 4px;
                               padding: 4px 8px; font-weight: 600; }}
QCalendarWidget QToolButton:hover {{ background: {shadow}; }}
QCalendarWidget QToolButton::menu-indicator {{ image: none; width: 0; }}
QCalendarWidget QAbstractItemView {{ selection-background-color: {a}; selection-color: white; outline: none; }}
QScrollArea#доска, QWidget#доска {{ background: transparent; border: none; }}
QPushButton#еще {{ background: transparent; border: none; border-radius: 6px; padding: 5px 6px; color: {secondary};
                  text-align: left; }}
QPushButton#еще:hover {{ background: {shadow}; color: palette(text); }}
"""
    )
