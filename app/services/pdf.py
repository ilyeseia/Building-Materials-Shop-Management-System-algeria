"""PDF generation with correct Arabic support.

The primary printable output of this application is **print-optimised HTML**,
which every browser renders with native Arabic shaping and needs no extra font
file.  PDF is a secondary format, offered for archiving and for systems that
cannot print HTML.

Generating Arabic PDF is not trivial and is done explicitly here:

1. a font that actually contains Arabic glyphs is located on the machine
   (Amiri, Arial Unicode, DejaVu Sans, …) — no font is redistributed with the
   code, and if none is found PDF generation reports that clearly instead of
   emitting tofu boxes;
2. ``arabic_reshaper`` joins the letters into their contextual forms;
3. ``python-bidi`` reorders the result for the right-to-left layout, because the
   PDF text model is left-to-right;
4. paragraphs and tables are right-aligned for Arabic and left-aligned for
   Latin scripts.

The output is a *business document*, not a certified or legally formatted
document; nothing here claims fiscal or metrological validity.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, A5
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

RTL_LANGUAGES = {"ar", "he", "fa", "ur"}

#: Candidate fonts in order of preference.  Arabic-first for correct shaping.
_FONT_CANDIDATES: tuple[tuple[str, str | None, str | None], ...] = (
    # (family name, regular path, bold path)
    ("Amiri", r"C:\Windows\Fonts\Amiri-Regular.ttf", r"C:\Windows\Fonts\Amiri-Bold.ttf"),
    ("DejaVuSans", r"C:\Windows\Fonts\DejaVuSans.ttf", r"C:\Windows\Fonts\DejaVuSans-Bold.ttf"),
    ("ArialUnicode", r"C:\Windows\Fonts\ARIALUNI.TTF", None),
    ("Arial", r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
    ("NotoNaskh", "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
     "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf"),
    ("DejaVuSansLinux", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
)

_registered: dict[str, tuple[str, str]] = {}


@dataclass(frozen=True)
class FontChoice:
    name: str
    regular: str
    bold: str | None

    @property
    def has_arabic(self) -> bool:
        return self.name in {"Amiri", "ArialUnicode", "NotoNaskh", "DejaVuSans", "DejaVuSansLinux"}


def _try_import_shaping():
    try:
        import arabic_reshaper  # type: ignore
        from bidi.algorithm import get_display  # type: ignore

        return arabic_reshaper, get_display
    except Exception:  # pragma: no cover - optional dependency
        return None, None


def shaping_available() -> bool:
    reshaper, bidi = _try_import_shaping()
    return reshaper is not None and bidi is not None


def find_font() -> FontChoice | None:
    """Return the first installed font usable for Arabic, or ``None``."""
    for name, regular, bold in _FONT_CANDIDATES:
        if regular and Path(regular).exists():
            return FontChoice(name=name, regular=regular, bold=bold if bold and Path(bold).exists() else None)
    return None


def register_fonts(choice: FontChoice | None = None) -> str | None:
    """Register the chosen font family with ReportLab; return its name."""
    choice = choice or find_font()
    if choice is None:
        return None
    if choice.name in _registered:
        return choice.name
    regular_name = f"{choice.name}"
    pdfmetrics.registerFont(TTFont(regular_name, choice.regular))
    bold_name = None
    if choice.bold:
        bold_name = f"{choice.name}-Bold"
        pdfmetrics.registerFont(TTFont(bold_name, choice.bold))
        try:
            from reportlab.pdfbase.pdfmetrics import registerFontFamily

            registerFontFamily(
                family=regular_name,
                normal=regular_name,
                bold=bold_name,
                italic=regular_name,
                boldItalic=bold_name,
            )
        except Exception:  # pragma: no cover - family registration is nice-to-have
            pass
    _registered[choice.name] = (regular_name, bold_name or regular_name)
    return regular_name


def pdf_available() -> bool:
    return find_font() is not None and shaping_available()


def shape_text(text: str, language: str = "ar") -> str:
    """Shape and reorder RTL text so ReportLab draws it correctly."""
    if text is None:
        return ""
    text = str(text)
    if language not in RTL_LANGUAGES:
        return text
    reshaper, get_display = _try_import_shaping()
    if reshaper is None or get_display is None:
        return text
    try:
        return get_display(reshaper.reshape(text))
    except Exception:  # pragma: no cover - malformed input must not crash printing
        return text


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

@dataclass
class PdfTable:
    headers: list[str]
    rows: list[list[str]]
    widths: list[float] | None = None
    align_right: bool = True


@dataclass
class PdfDocument:
    title: str
    subtitle: str | None = None
    meta: list[tuple[str, str]] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)
    tables: list[PdfTable] = field(default_factory=list)
    totals: list[tuple[str, str]] = field(default_factory=list)
    footer: str | None = None
    language: str = "ar"
    page_size: str = "A4"


def _styles(font_name: str, rtl: bool):
    base = getSampleStyleSheet()
    align = TA_RIGHT if rtl else TA_LEFT
    title = ParagraphStyle(
        "DocTitle", parent=base["Title"], fontName=font_name, fontSize=18,
        leading=24, alignment=TA_CENTER,
    )
    subtitle = ParagraphStyle(
        "DocSubtitle", parent=base["Normal"], fontName=font_name, fontSize=11,
        leading=16, alignment=TA_CENTER, textColor=colors.HexColor("#444444"),
    )
    normal = ParagraphStyle(
        "DocNormal", parent=base["Normal"], fontName=font_name, fontSize=10,
        leading=15, alignment=align,
    )
    cell = ParagraphStyle(
        "DocCell", parent=base["Normal"], fontName=font_name, fontSize=9,
        leading=12, alignment=align,
    )
    header = ParagraphStyle(
        "DocHeader", parent=cell, fontSize=9.5, textColor=colors.white,
    )
    return {
        "title": title,
        "subtitle": subtitle,
        "normal": normal,
        "cell": cell,
        "header": header,
    }


def build_pdf(document: PdfDocument) -> bytes:
    """Render a :class:`PdfDocument` to PDF bytes.

    Raises :class:`RuntimeError` when no Arabic-capable font or no shaping
    library is available — a caller that needs a printable Arabic document
    should then fall back to the HTML print view, which never has this problem.
    """
    font_name = register_fonts()
    if font_name is None:
        raise RuntimeError(
            "No Arabic-capable TrueType font was found on this system. "
            "Install Amiri or Arial Unicode, or use the HTML print view."
        )
    rtl = document.language in RTL_LANGUAGES
    if rtl and not shaping_available():
        raise RuntimeError(
            "Arabic PDF requires the 'arabic-reshaper' and 'python-bidi' packages. "
            "Install them, or use the HTML print view."
        )

    styles = _styles(font_name, rtl)
    size = A5 if document.page_size.upper() == "A5" else A4
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=size,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=document.title,
    )

    story: list = []
    story.append(Paragraph(shape_text(document.title, document.language), styles["title"]))
    if document.subtitle:
        story.append(Paragraph(shape_text(document.subtitle, document.language), styles["subtitle"]))
    story.append(Spacer(1, 6))

    if document.meta:
        meta_rows = [
            [Paragraph(shape_text(label, document.language), styles["cell"]),
             Paragraph(shape_text(value, document.language), styles["cell"])]
            for label, value in document.meta
        ]
        meta_table = Table(meta_rows, colWidths=[45 * mm, None])
        meta_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#DDDDDD")),
        ]))
        story.append(meta_table)
        story.append(Spacer(1, 8))

    for text in document.paragraphs:
        story.append(Paragraph(shape_text(text, document.language), styles["normal"]))
        story.append(Spacer(1, 4))

    for table in document.tables:
        headers = [Paragraph(shape_text(head, document.language), styles["header"]) for head in table.headers]
        data = [headers] + [
            [Paragraph(shape_text(cell, document.language), styles["cell"]) for cell in row]
            for row in table.rows
        ]
        rendered = Table(data, colWidths=table.widths, repeatRows=1)
        rendered.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f5d50")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#BBBBBB")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "RIGHT" if rtl else "LEFT"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F6F5")]),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(rendered)
        story.append(Spacer(1, 8))

    if document.totals:
        total_rows = [
            [Paragraph(shape_text(label, document.language), styles["cell"]),
             Paragraph(shape_text(value, document.language), styles["cell"])]
            for label, value in document.totals
        ]
        totals_table = Table(total_rows, colWidths=[None, 45 * mm], hAlign="RIGHT" if rtl else "LEFT")
        totals_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("ALIGN", (0, 0), (0, -1), "RIGHT" if rtl else "LEFT"),
            ("ALIGN", (1, 0), (1, -1), "LEFT" if rtl else "RIGHT"),
            ("LINEABOVE", (0, 0), (-1, 0), 0.5, colors.HexColor("#2f5d50")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(totals_table)

    if document.footer:
        story.append(Spacer(1, 12))
        story.append(Paragraph(shape_text(document.footer, document.language), styles["subtitle"]))

    doc.build(story)
    return buffer.getvalue()


def build_simple_pdf(*, title: str, lines: list[str], language: str = "ar") -> bytes:
    """Convenience helper: a title followed by plain lines."""
    return build_pdf(
        PdfDocument(title=title, paragraphs=list(lines), language=language)
    )
