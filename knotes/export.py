# SPDX-License-Identifier: GPL-3.0-only
"""The minutes and the transcript as a PDF or Word (DOCX) file, for the client and the archive.

The minutes are the email text (minutes.for_email: no time marks, no analyst section): the heading, the greeting,
the participants, sections as headings, numbered items with who and when in bold. The transcript: title, date,
duration and participants, then each speaker's phrases with their time; the ★ marks and the analyst's notes are left
out, they are the analyst's own and stay in the program.

Both files are drawn from one plain document (Document) with one template (TEMPLATE), so an owner's template replaces
it in one place. The PDF is laid out and printed by Qt; the DOCX is written here, a zip of a few XML parts: no extra
library in the build.
"""

import dataclasses
import datetime
import errno
import io
import os
import pathlib
import re
import zipfile
from xml.sax.saxutils import escape

from PySide6.QtCore import QMarginsF, QPointF, QRectF, QSizeF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QPageLayout,
    QPageSize,
    QPainter,
    QPdfWriter,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextOption,
)

from knotes import VERSION, catalog, failures, minutes, transcript
from knotes.language import tr

TITLE, HEADING, TEXT, ITEM, SPEAKER, PHRASE = "название", "раздел", "текст", "пункт", "говорящий", "фраза"
PDF, DOCX = ".pdf", ".docx"
RESOLUTION = 300  # dots per inch the PDF is laid out in; the text itself stays vector
NOT_IN_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')  # characters Windows does not allow in a file name
NOT_IN_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
BOLD = re.compile(r"\*\*(.+?)\*\*")


@dataclasses.dataclass(frozen=True)
class Template:
    """How the files look: sizes in points, distances in millimetres, colors as #RRGGBB."""

    font: str = "Segoe UI"  # has Cyrillic on every Windows; elsewhere a close font is taken
    size: float = 11
    title_size: float = 18
    heading_size: float = 13.5
    footer_size: float = 9  # page numbers
    margin_mm: float = 20
    line_spacing: float = 1.15
    space_after: float = 6  # after a paragraph
    heading_space: float = 14  # before a section heading or a speaker
    item_indent_mm: float = 7  # of the item text after its number
    phrase_indent_mm: float = 16  # of the phrase text after its time
    text_color: str = "#1F1F1F"
    quiet_color: str = "#6F6E69"  # numbers, times, labels
    heading_color: str = "#0F6E68"


TEMPLATE = Template()


@dataclasses.dataclass(frozen=True)
class Run:
    text: str
    bold: bool = False
    quiet: bool = False


@dataclasses.dataclass(frozen=True)
class Block:
    kind: str  # TITLE, HEADING, TEXT, ITEM, SPEAKER, PHRASE
    runs: tuple[Run, ...]
    number: str = ""  # "1." of an item, the time of a phrase


@dataclasses.dataclass(frozen=True)
class Document:
    title: str
    blocks: tuple[Block, ...]

    def text(self) -> str:
        return "\n".join(" ".join(filter(None, (b.number, "".join(r.text for r in b.runs)))) for b in self.blocks)


# --- what goes into the file ---------------------------------------------------------------------------------


def participant_caption(who: str) -> str:
    """Recognition caption ("Я", "Собеседник 2", "Участник 3") in the interface language; in the data it is Russian."""
    word, _, number = who.rpartition(" ")
    if who == transcript.ME:
        return tr("Me")
    if word == "Собеседник" and number.isdigit():
        return tr("Speaker {n}", n=number)
    if word == "Участник" and number.isdigit():
        return tr("Participant {n}", n=number)
    return who


def _runs(text: str, bold: bool = False) -> tuple[Run, ...]:
    """**Bold** of the minutes as bold runs."""
    parts = BOLD.split(text)
    return tuple(Run(part, bold or number % 2 == 1) for number, part in enumerate(parts) if part)


def _start(start: str) -> list[Block]:
    blocks = []
    for line in (s.strip() for s in start.splitlines()):
        if line.startswith("#"):
            blocks.append(Block(TITLE, _runs(line.lstrip("# "))))
        elif found := minutes.participants(line):
            blocks.append(Block(TEXT, (Run(f"{found[0]} - ", quiet=True), *_runs(found[1]))))
        elif line:
            blocks.append(Block(TEXT, _runs(line)))
    return blocks


def _items(section: minutes.Section) -> list[Block]:
    blocks = []
    for item in minutes.items(section):
        if item.number:
            who = (Run(" "), *_runs(item.who, bold=True)) if item.who else ()
            blocks.append(Block(ITEM, (*_runs(item.text), *who), f"{item.number}."))
        else:
            blocks.append(Block(TEXT, _runs(re.sub(r"^[-*] ", "• ", item.text))))
    return blocks


def minutes_document(text: str, title: str) -> Document:
    """The minutes as they go to the client by email; title: when the minutes have no heading of their own."""
    start, sections = minutes.sections(minutes.for_email(text))
    blocks = _start(start)
    if not any(b.kind == TITLE for b in blocks):
        blocks.insert(0, Block(TITLE, (Run(title),)))
    for section in sections:
        blocks.append(Block(HEADING, _runs(section.heading)))
        blocks += _items(section)
    heading = next(b for b in blocks if b.kind == TITLE)
    return Document("".join(r.text for r in heading.runs), tuple(blocks))


def _fact(label: str, value: str) -> Block:
    return Block(TEXT, (Run(f"{label}: ", quiet=True), Run(value)))


def transcript_document(folder: pathlib.Path) -> Document:
    """Title, date, duration, participants, then the phrases of each speaker in turn with their time."""
    data = transcript.phrases(folder)
    if data is None:
        raise FileNotFoundError(errno.ENOENT, "the meeting is not transcribed", str(folder / transcript.PHRASES))
    names, title = transcript.names(folder), catalog.display_title(folder)
    blocks = [Block(TITLE, (Run(title),)), _fact(tr("Дата"), f"{catalog.folder_date(folder):%d.%m.%Y, %H:%M}")]
    if seconds := data["сведения"].get("длительность"):
        blocks.append(_fact(tr("Длительность"), catalog.duration(seconds)))
    if speakers := [names.get(who) or participant_caption(who) for who in transcript.participants(folder)]:
        blocks.append(_fact(tr("Participants"), ", ".join(speakers)))
    speaking = None
    for block in (b for b in transcript.blocks(folder) if "кто" in b):  # marks are the analyst's, not for the file
        if block["кто"] != speaking:  # a mark left out no longer splits one speaker's turn
            speaking = block["кто"]
            blocks.append(Block(SPEAKER, (Run(names.get(speaking) or participant_caption(speaking), True),)))
        blocks += [Block(PHRASE, (Run(text),), transcript.time(start)) for _, start, text in block["фразы"]]
    return Document(title, tuple(blocks))


def file_name(kind: str, folder: pathlib.Path) -> str:
    """ "Протокол - Согласование загрузки - 14.10.2026": what, which meeting and when, without characters Windows
    does not allow in a file name; without the extension."""
    title = catalog.display_title(folder)
    date = f"{catalog.folder_date(folder):%d.%m.%Y}"
    name = f"{kind} - {title}" + ("" if date in title else f" - {date}")
    return NOT_IN_NAME.sub("-", name)[:150].strip(" .-")


def with_extension(path: pathlib.Path, extension: str) -> pathlib.Path:
    """A name typed without .pdf or .docx gets the extension of the type chosen in the dialog."""
    return path if path.suffix.lower() in (PDF, DOCX) else path.with_name(path.name + extension)


def save(document: Document, path: pathlib.Path, template: Template = TEMPLATE) -> None:
    """PDF or DOCX by the extension; a file of that name is replaced only once the new one is complete."""
    kind = path.suffix.lower()
    if kind not in (PDF, DOCX):
        raise ValueError(f"not a PDF or DOCX file: {path.name}")
    temporary = path.with_name(f"~{path.stem}.{os.getpid()}{kind}")
    try:
        if kind == PDF:
            _write_pdf(document, temporary, template)
        else:
            temporary.write_bytes(docx(document, template))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def failure_text(error: OSError, path: pathlib.Path) -> str:
    if isinstance(error, PermissionError):
        return tr(
            "Couldn't save {file}: it is open in another program or the folder is read-only. Close it or choose "
            "another name or folder.",
            file=path.name,
        )
    return tr("Couldn't save the file. {reason}", reason=failures.text(error))


# --- PDF -----------------------------------------------------------------------------------------------------


def _font(template: Template, size: float) -> QFont:
    font = QFont(template.font)
    font.setPointSizeF(size)
    return font


def _char_format(block: Block, template: Template) -> QTextCharFormat:
    size = {TITLE: template.title_size, HEADING: template.heading_size}.get(block.kind, template.size)
    result = QTextCharFormat()
    result.setFontFamilies([template.font])
    result.setFontPointSize(size)
    result.setForeground(QColor(template.heading_color if block.kind == HEADING else template.text_color))
    if block.kind in (TITLE, HEADING):
        result.setFontWeight(QFont.Weight.DemiBold)
    return result


def _block_format(block: Block, template: Template, dots: float, scale: float) -> QTextBlockFormat:
    """dots: of the PDF in a point. Qt takes the left margin in dots of the PDF, but tab stops and the space above
    and below a paragraph in screen pixels: those are divided by scale."""
    result = QTextBlockFormat()
    result.setLineHeight(template.line_spacing * 100, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
    result.setBottomMargin(template.space_after * dots / scale)
    if block.kind in (HEADING, SPEAKER):
        result.setTopMargin(template.heading_space * dots / scale)
    if block.number:
        indent = (template.item_indent_mm if block.kind == ITEM else template.phrase_indent_mm) / 25.4 * 72 * dots
        result.setLeftMargin(indent)
        result.setTextIndent(-indent)
        result.setTabPositions([QTextOption.Tab(indent / scale, QTextOption.TabType.LeftTab)])
    return result


def pdf_writer(path: pathlib.Path, document: Document, template: Template = TEMPLATE) -> QPdfWriter:
    """An A4 PDF with the margins of the template."""
    writer = QPdfWriter(str(path))
    writer.setResolution(RESOLUTION)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    margin = template.margin_mm
    writer.setPageMargins(QMarginsF(margin, margin, margin, margin), QPageLayout.Unit.Millimeter)
    writer.setTitle(document.title)
    writer.setCreator(f"QG Knotes {VERSION}")
    return writer


def pdf_layout(document: Document, writer: QPdfWriter, template: Template = TEMPLATE) -> QTextDocument:
    """The document laid out by Qt in pages of the PDF, in its dots; under each page there is room for its
    number."""
    result = QTextDocument()
    result.documentLayout().setPaintDevice(writer)
    result.setDocumentMargin(0)
    result.setDefaultFont(_font(template, template.size))
    dots = writer.logicalDpiY() / 72
    screen = QGuiApplication.primaryScreen()
    scale = writer.logicalDpiY() / (round(screen.logicalDotsPerInchY()) if screen else 100)
    cursor = QTextCursor(result)
    for number, block in enumerate(document.blocks):
        characters = _char_format(block, template)
        if number:
            cursor.insertBlock(_block_format(block, template, dots, scale), characters)
        else:
            cursor.setBlockFormat(_block_format(block, template, dots, scale))
            cursor.setBlockCharFormat(characters)
        for run in ((Run(f"{block.number}\t", quiet=True),) if block.number else ()) + block.runs:
            look = QTextCharFormat(characters)
            if run.bold:
                look.setFontWeight(QFont.Weight.Bold)
            if run.quiet:
                look.setForeground(QColor(template.quiet_color))
            cursor.insertText(run.text, look)
    page = writer.pageLayout().paintRectPixels(writer.resolution())
    result.setPageSize(QSizeF(page.width(), page.height() - template.footer_size * 2.5 * dots))
    _keep_with_next(result, [b.kind for b in document.blocks])
    return result


def _keep_with_next(layout: QTextDocument, kinds: list[str]) -> None:
    """A section heading or a speaker's name left at the bottom of a page goes to the next one, with what follows."""
    height, block = layout.pageSize().height(), layout.begin()
    for kind in kinds:
        following = block.next()
        if kind in (HEADING, SPEAKER) and following.isValid():
            top = layout.documentLayout().blockBoundingRect(block).top()
            first_line = layout.documentLayout().blockBoundingRect(following).top()
            if following.layout().lineCount():
                first_line += following.layout().lineAt(0).position().y()
            if int(first_line // height) > int(top // height):
                look = block.blockFormat()
                look.setPageBreakPolicy(QTextBlockFormat.PageBreakFlag.PageBreak_AlwaysBefore)
                QTextCursor(block).setBlockFormat(look)
                # without it Qt lays out the rest from stale positions and pages go missing
                layout.markContentsDirty(block.position(), layout.characterCount() - block.position())
        block = following


def _write_pdf(document: Document, path: pathlib.Path, template: Template) -> None:
    writer = pdf_writer(path, document, template)
    layout = pdf_layout(document, writer, template)
    area = QRectF(QPointF(0, 0), layout.pageSize())
    painter = QPainter()
    if not painter.begin(writer):
        raise OSError(errno.EACCES, "the PDF file cannot be written", str(path))
    try:
        count = layout.pageCount()
        for number in range(count):
            if number:
                writer.newPage()
            _page(painter, layout, number, area, (count, template))
    finally:
        painter.end()


def _page(painter: QPainter, layout: QTextDocument, number: int, area: QRectF, of: tuple[int, Template]) -> None:
    """One page of the layout and its number under it, "2 / 5"."""
    count, template = of
    painter.save()
    painter.setClipRect(area)  # a line pushed to the next page is not drawn under this one
    painter.translate(0, -number * area.height())
    # a dot in from the page edges: the lines of the next page are not put into the text of this one
    layout.drawContents(painter, area.translated(0, number * area.height()).adjusted(0, 1, 0, -1))
    painter.restore()
    painter.setFont(_font(template, template.footer_size))
    painter.setPen(QColor(template.quiet_color))
    below = QRectF(0, area.height(), area.width(), painter.device().height() - area.height())
    painter.drawText(below, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, f"{number + 1} / {count}")


# --- DOCX ----------------------------------------------------------------------------------------------------

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
CONTENT_TYPES = (
    HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '<Override PartName="/word/footer1.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    "</Types>"
)
PACKAGE_LINKS = (
    HEAD + f'<Relationships xmlns="{RELATIONSHIPS}">'
    f'<Relationship Id="rId1" Type="{OFFICE}/officeDocument" Target="word/document.xml"/>'
    f'<Relationship Id="rId2" Type="{RELATIONSHIPS}/metadata/core-properties" Target="docProps/core.xml"/>'
    "</Relationships>"
)
DOCUMENT_LINKS = (
    HEAD + f'<Relationships xmlns="{RELATIONSHIPS}">'
    f'<Relationship Id="rId1" Type="{OFFICE}/styles" Target="styles.xml"/>'
    f'<Relationship Id="rId2" Type="{OFFICE}/footer" Target="footer1.xml"/>'
    "</Relationships>"
)


def _twips(points: float) -> int:
    return round(points * 20)


def _mm(millimetres: float) -> int:
    return round(millimetres / 25.4 * 1440)


def _color(color: str) -> str:
    return color.removeprefix("#").upper()


def _xml(text: str) -> str:
    return escape(NOT_IN_XML.sub("", text.replace("\t", " ")), {'"': "&quot;"})


def _run(run: Run, template: Template) -> str:
    look = ("<w:b/><w:bCs/>" if run.bold else "") + (
        f'<w:color w:val="{_color(template.quiet_color)}"/>' if run.quiet else ""
    )
    return f'<w:r>{f"<w:rPr>{look}</w:rPr>" if look else ""}<w:t xml:space="preserve">{_xml(run.text)}</w:t></w:r>'


def _paragraph(block: Block, template: Template) -> str:
    look = {TITLE: '<w:pStyle w:val="Title"/>', HEADING: '<w:pStyle w:val="Heading1"/>'}.get(block.kind, "")
    if block.kind == SPEAKER:
        look += f'<w:keepNext/><w:spacing w:before="{_twips(template.heading_space)}"/>'
    runs = "".join(_run(run, template) for run in block.runs)
    if block.number:
        indent = _mm(template.item_indent_mm if block.kind == ITEM else template.phrase_indent_mm)
        look += (
            f'<w:tabs><w:tab w:val="left" w:pos="{indent}"/></w:tabs><w:ind w:left="{indent}" w:hanging="{indent}"/>'
        )
        runs = _run(Run(block.number, quiet=True), template) + "<w:r><w:tab/></w:r>" + runs
    return f"<w:p>{f'<w:pPr>{look}</w:pPr>' if look else ''}{runs}</w:p>"


def _styles(template: Template) -> str:
    font, text = _xml(template.font), _color(template.text_color)
    fonts = f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:eastAsia="{font}" w:cs="{font}"/>'
    size = round(template.size * 2)
    heading = (
        f'<w:pPr><w:keepNext/><w:spacing w:before="{_twips(template.heading_space)}" w:after="{_twips(3)}"/>'
        f'<w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:color w:val="{_color(template.heading_color)}"/>'
        f'<w:sz w:val="{round(template.heading_size * 2)}"/></w:rPr>'
    )
    return (
        HEAD + f'<w:styles xmlns:w="{W}"><w:docDefaults><w:rPrDefault><w:rPr>{fonts}<w:color w:val="{text}"/>'
        f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>'
        f'<w:spacing w:after="{_twips(template.space_after)}" w:line="{round(240 * template.line_spacing)}" '
        'w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>'
        f'<w:next w:val="Normal"/><w:pPr><w:spacing w:after="{_twips(8)}"/></w:pPr><w:rPr><w:b/>'
        f'<w:sz w:val="{round(template.title_size * 2)}"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
        f'<w:next w:val="Normal"/>{heading}</w:style></w:styles>'
    )


def _footer(template: Template) -> str:
    """ "2 / 5" on the right: the page and the page count, fields Word fills in."""
    look = f'<w:rPr><w:color w:val="{_color(template.quiet_color)}"/><w:sz w:val="{round(template.footer_size * 2)}"/>'
    look += "</w:rPr>"
    page, pages = (
        f'<w:fldSimple w:instr=" {code} "><w:r>{look}<w:t>1</w:t></w:r></w:fldSimple>' for code in ("PAGE", "NUMPAGES")
    )
    return (
        HEAD + f'<w:ftr xmlns:w="{W}"><w:p><w:pPr><w:jc w:val="right"/></w:pPr>{page}'
        f'<w:r>{look}<w:t xml:space="preserve"> / </w:t></w:r>{pages}</w:p></w:ftr>'
    )


def _document_xml(document: Document, template: Template) -> str:
    margin = _mm(template.margin_mm)
    section = (
        '<w:sectPr><w:footerReference w:type="default" r:id="rId2"/><w:pgSz w:w="11906" w:h="16838"/>'
        f'<w:pgMar w:top="{margin}" w:right="{margin}" w:bottom="{margin}" w:left="{margin}" '
        f'w:header="{margin // 2}" w:footer="{margin // 2}" w:gutter="0"/></w:sectPr>'
    )
    body = "".join(_paragraph(block, template) for block in document.blocks)
    return HEAD + f'<w:document xmlns:w="{W}" xmlns:r="{OFFICE}"><w:body>{body}{section}</w:body></w:document>'


def _core(document: Document) -> str:
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        HEAD + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{_xml(document.title)}</dc:title><dc:creator>QG Knotes {VERSION}</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created></cp:coreProperties>'
    )


def docx(document: Document, template: Template = TEMPLATE) -> bytes:
    """A Word document: the text, the styles of the template, page numbers in the footer."""
    parts = {
        "[Content_Types].xml": CONTENT_TYPES,
        "_rels/.rels": PACKAGE_LINKS,
        "docProps/core.xml": _core(document),
        "word/_rels/document.xml.rels": DOCUMENT_LINKS,
        "word/document.xml": _document_xml(document, template),
        "word/styles.xml": _styles(template),
        "word/footer1.xml": _footer(template),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, text in parts.items():
            package.writestr(name, text.encode("utf-8"))
    return buffer.getvalue()
