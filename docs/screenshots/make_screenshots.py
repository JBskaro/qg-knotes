# SPDX-License-Identifier: GPL-3.0-only
"""README screenshots and the demo GIF from the real windows, on made-up meetings (demo_data.py) in a throwaway profile:
the user's settings, recordings and keys are never touched.

The page shows pictures about 1000 points wide, so the window is kept narrow and shot at the screen's own resolution;
each feature is a close-up of its part of the window, not the whole window shrunk.

Usage, from the repository folder:
    .venv\\Scripts\\python.exe docs\\screenshots\\make_screenshots.py ru
    .venv\\Scripts\\python.exe docs\\screenshots\\make_screenshots.py en
Pictures go to docs/images/<language>/.
"""

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
LANGUAGE = sys.argv[1] if len(sys.argv) > 1 else "ru"
OUT = ROOT / "docs" / "images" / LANGUAGE
WIDTH, HEIGHT = 1200, 720  # window size in points for the pictures
GIF_SIZE, FPS = (1080, 680), 10  # narrower than this folds the sections column into icons
HERO_SHARE = 0.86  # the main window takes this part of the header width
SUBTITLE = {
    "ru": "Протокол встречи без бота. Речь распознается на вашем компьютере, бесплатно.",
    "en": "Meeting minutes without a bot. Speech recognized on your PC, for free.",
}

USER_ENV = dict(os.environ)  # before the throwaway profile: Pillow for the GIF may live in the user's own folders
profile = pathlib.Path(tempfile.mkdtemp(prefix="knotes_demo_"))
for name in ("app/QG Knotes", "local", "temp"):
    (profile / name).mkdir(parents=True)
os.environ.update(
    APPDATA=str(profile / "app"),
    LOCALAPPDATA=str(profile / "local"),
    TEMP=str(profile / "temp"),
    TMP=str(profile / "temp"),
    USERNAME="demo",
)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs" / "screenshots"))

import keyring  # noqa: E402
import keyring.backends.null  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QRegion  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QListWidgetItem, QSystemTrayIcon, QWidget  # noqa: E402

import demo_data  # noqa: E402
import frame  # noqa: E402

keyring.set_keyring(keyring.backends.null.Keyring())  # no keys from the real store
QSystemTrayIcon.show = lambda _self: None  # no icon by the user's clock
QSystemTrayIcon.setVisible = lambda _self, _visible: None
records = profile / "records"
demo_data.build(records, LANGUAGE)
demo = demo_data.content(LANGUAGE)
PROJECT = next(iter(demo.PROJECTS))  # the project the shots and the GIF open
(profile / "app" / "QG Knotes" / "настройки.json").write_text(
    json.dumps(demo_data.settings(records, LANGUAGE), ensure_ascii=False), encoding="utf-8"
)

from knotes import app as knotes_app  # noqa: E402
from knotes.ui.projects_panel import ALL  # noqa: E402

qt = QApplication([])
qt.setQuitOnLastWindowClosed(False)
knotes_app._translator(qt)
program = knotes_app.App(on_autostart=False)
program.startup()
window = program.window
window.move(-4000, -4000)  # off the screen: the shots are taken from the widgets
window.resize(WIDTH, HEIGHT)
window.show()
SCALE = 2  # pictures at twice the points, whatever the screen: on a 125% monitor they came out smaller
frames: list[QImage] = []
clicks: list[tuple[QPoint, int]] = []  # where a click is drawn and on how many more frames


def wait(seconds: float = 0.4) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qt.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        time.sleep(0.01)


def render(widget: QWidget, area: QRect | None = None, scale: float = SCALE) -> QImage:
    """The widget, or an area of it in points, drawn at the given scale (not the screen's)."""
    area = area or widget.rect()
    image = QImage(int(area.width() * scale), int(area.height() * scale), QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(scale)
    image.fill(Qt.GlobalColor.transparent)
    widget.render(image, QPoint(0, 0), QRegion(area))
    image.setDevicePixelRatio(1)
    return image


def picture(part: QWidget | None = None, width: int = 0) -> QImage:
    """The window or one part of it; width cuts the part to so many points."""
    if part is None:
        return render(window)
    corner = part.mapTo(window, QPoint(0, 0))
    return render(window, QRect(corner.x(), corner.y(), width or part.width(), part.height()))


def shot(name: str, part: QWidget | None = None, width: int = 0) -> QImage:
    OUT.mkdir(parents=True, exist_ok=True)
    image = picture(part, width)
    frame.framed(image, SCALE).save(str(OUT / f"{name}.png"))
    print("saved", name, image.width(), "x", image.height())
    return image


def _frame() -> None:
    image = render(window, scale=1)
    if clicks:
        point, left = clicks[0]
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(15, 110, 104, 220), 3))
        painter.setBrush(QColor(15, 110, 104, 70))
        painter.drawEllipse(point, 16, 16)
        painter.end()
        clicks[0] = (point, left - 1)
        if left <= 1:
            clicks.pop(0)
    frames.append(image)


def show_click(widget: QWidget, at: QPoint | None = None) -> None:
    """The circle of a click over a widget on the next frames of the GIF."""
    point = widget.mapTo(window, at if at is not None else widget.rect().center())
    clicks.append((point, 6))


def meeting_row(folder: str) -> QListWidgetItem:
    list_ = window.list_
    for number in range(list_.count()):
        kind = list_.item(number).data(Qt.ItemDataRole.UserRole)
        if kind and kind.meeting.folder.name == folder:
            return list_.item(number)
    raise LookupError(folder)


def click_row(folder: str) -> None:
    list_, row = window.list_, meeting_row(folder)
    center = list_.visualItemRect(row).center()
    show_click(list_.viewport(), center)
    wait(0.5)
    QTest.mouseClick(list_.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
    wait(1)


def click_project(code: str) -> None:
    panel = window.panel
    row = next(panel.item(n) for n in range(panel.count()) if panel.item(n).data(Qt.ItemDataRole.UserRole) == code)
    center = panel.visualItemRect(row).center()
    show_click(panel.viewport(), center)
    wait(0.5)
    QTest.mouseClick(panel.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
    wait(1)


def label_with(text: str) -> QLabel:
    return next(label for label in window.findChildren(QLabel) if text in label.text() and label.isVisible())


def logo() -> None:
    """The program icon for the page header, from the same drawing as the icon by the clock."""
    from knotes.ui import style  # noqa: PLC0415 - after the QApplication

    (ROOT / "docs" / "images").mkdir(parents=True, exist_ok=True)
    style.tray_icon(False).pixmap(256, 256).save(str(ROOT / "docs" / "images" / "logo.png"))


def _offscreen(widget: QWidget, width: int = 0, height: int = 0) -> QImage:
    widget.move(-4000, -3000)
    if width:
        widget.resize(width, height)
    widget.show()
    wait()
    image = render(widget)
    widget.hide()
    solid = QImage(image.size(), QImage.Format.Format_ARGB32_Premultiplied)
    solid.fill(QColor("#202020"))  # the bar and the notepad are see-through on screen: not over the minutes here
    painter = QPainter(solid)
    painter.drawImage(0, 0, image)
    painter.end()
    return solid


def hero(main: QImage) -> None:
    """The page header: the meetings window with minutes, the recording bar over its top edge and the meeting notepad
    over its corner, all at the screen's resolution."""
    from knotes.ui.notepad import NotepadWindow  # noqa: PLC0415 - after the QApplication
    from knotes.ui.recording_bar import RecordingBar  # noqa: PLC0415

    bar = RecordingBar(False, "Ctrl+Alt+R", "Ctrl+Alt+M", "Ctrl+Alt+N")
    bar.refresh(754, {"микрофон": 72, "собеседники": 58})
    bar_image = _offscreen(bar)
    pad = NotepadWindow(records / demo.MEETINGS[0]["folder"], False, list(demo.PROJECTS), lambda: 754.0)
    pad_image = _offscreen(pad, 400, 330)
    width = int(main.width() / HERO_SHARE)
    side, top = int(30 * SCALE), int(34 * SCALE)
    main_rect = QRectF(side, top, main.width(), main.height())
    bar_rect = QRectF(
        main_rect.center().x() - bar_image.width() / 2,
        top - bar_image.height() / 2,
        bar_image.width(),
        bar_image.height(),
    )
    pad_rect = QRectF(
        width - side - pad_image.width(),
        main_rect.bottom() - pad_image.height() + 40 * SCALE,
        pad_image.width(),
        pad_image.height(),
    )
    canvas = frame.compose(
        width, int(pad_rect.bottom() + top), [(main, main_rect), (pad_image, pad_rect), (bar_image, bar_rect)], SCALE
    )
    canvas.save(str(OUT / "hero.png"))
    print("saved hero", canvas.width(), "x", canvas.height())


def social(minutes: QImage) -> None:
    """The repository preview for links in messengers: 1280x640, the title over a close-up of the minutes."""
    close_up = minutes.scaledToWidth(1180, Qt.TransformationMode.SmoothTransformation)
    full = frame.framed(close_up, 1.6, "QG Knotes", SUBTITLE[LANGUAGE])
    full = full.scaledToWidth(1280, Qt.TransformationMode.SmoothTransformation)
    full.copy(QRect(0, 0, 1280, 640)).save(str(OUT / "social.png"))
    print("saved social")


def stills() -> None:
    first = demo.MEETINGS[0]["folder"]
    click_row(first)
    hero(picture())
    minutes = shot("minutes", window.details)
    social(minutes)
    program.open_place(records / first, 425)
    wait(1)
    shot("transcript", window.details)
    click_project(PROJECT)
    shot("project", window.details)
    click_project(ALL)
    program.open_tasks(None)
    wait(1)
    shot("tasks", window.right_)
    program.open_summary()
    wait(1.5)
    shot("summary", window.right_)
    program.open_help()
    wait()
    page = window.page
    page.topics.button(2).click()
    question = next(q for _, q in page.questions if q.answer.question == "Как составить протокол?")
    question.set_open(True)
    wait()
    shot("help", window.right_, min(window.right_.width(), 760))
    window.go_back()
    click_project(ALL)


def _pillow_python() -> str:
    """An interpreter with Pillow for make_gif.py: this one, or the Python on PATH (the app's environment has none)."""
    if importlib.util.find_spec("PIL") is not None:
        return sys.executable
    return shutil.which("python") or shutil.which("py") or sys.executable


def gif() -> None:
    """About 15 seconds: the list, a meeting's minutes, a click on a time into the transcript, the project page and
    the task board. Frames are the window at its size in points: sharp, not shrunk."""
    window.resize(*GIF_SIZE)
    window.close_meeting()
    wait(1)
    timer = QTimer()
    timer.timeout.connect(_frame)
    timer.start(1000 // FPS)
    first = demo.MEETINGS[0]["folder"]
    wait(1.5)
    click_row(first)
    wait(2.5)
    show_click(label_with("07:05"))
    wait(0.5)
    program.open_place(records / first, 425)
    wait(2.5)
    click_project(PROJECT)
    wait(2.5)
    show_click(window.projects_column, QPoint(80, 140))
    wait(0.5)
    program.open_tasks(PROJECT)
    wait(3)
    timer.stop()
    folder = pathlib.Path(tempfile.gettempdir()) / "knotes_gif_frames"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir()
    for number, image in enumerate(frames):
        frame.framed(image).save(str(folder / f"{number:04d}.png"))
    subprocess.run(
        [
            _pillow_python(),
            str(ROOT / "docs" / "screenshots" / "make_gif.py"),
            str(folder),
            str(OUT / "demo.gif"),
            str(1000 // FPS),
        ],
        check=True,
        env=USER_ENV,
    )
    shutil.rmtree(folder, ignore_errors=True)
    print("saved demo.gif,", len(frames), "frames")


wait(1.5)
program._refresh_window(always=True)  # the first list fill comes from a timer: do it now
wait(0.5)
logo()
stills()
gif()
program.quit_app()
wait(0.3)
shutil.rmtree(profile, ignore_errors=True)
