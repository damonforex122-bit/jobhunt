"""Render a Resume to PDF in the layout of the master (Google Docs export, US Letter, Arial)."""

from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer

from jobhunt.tailor.model import Resume, Run

# --- Layout constants, measured from the master PDF (points; 72 = 1 inch) -------------------
FONT_DIR = Path("/System/Library/Fonts/Supplemental")
PAGE_SIZE = letter  # 612 x 792
MARGIN_LEFT = MARGIN_RIGHT = 72.0
MARGIN_TOP = 71.6  # name baseline lands at y=709.4 (a paragraph's first baseline is fontSize below its top)
MARGIN_BOTTOM = 72.0

SIZE_NAME = 11.0  # bold, centered
SIZE_CONTACT = 10.0  # centered, parts separated by " | "
SIZE_HEADING = 10.0  # bold, UPPERCASE, no rule line
SIZE_BODY = 10.0  # org/title/bullets/summary alike (org bold, title italic)
SIZE_BULLET_GLYPH = 11.0
BULLET_GLYPH = "●"
BULLET_GLYPH_FALLBACK = "•"  # Helvetica has no U+25CF

LEADING = 15.0  # summary, org, title, headings
LEADING_BULLET = 15.5  # bullet lines (the 11pt glyph stretches the line box)
LEADING_NAME = 16.5  # name -> contact baseline is 15.5 in the master
LEADING_CONTACT = 15.0

SPACE_BEFORE_SECTION = 9.8  # heading baseline is ~24.8 below the previous baseline
SPACE_BEFORE_ENTRY = 6.2  # org line is ~21.7 below the last bullet
SPACE_BEFORE_FIRST_BULLET = 4.0  # title -> first bullet is ~19 (vs 15.5 line to line)
SPACE_BEFORE_BULLET = 0.4  # bullet -> bullet is ~16 vs 15.5 for a wrapped line
SPACE_BEFORE_BLURB = 3.0  # title -> blurb is 18
SPACE_BEFORE_BULLET_AFTER_BLURB = 3.2  # blurb -> bullet is 18.2

BULLET_INDENT = 18.0  # bullet at x=72, text (and wrapped lines) at x=90: hanging indent
# Org + location and title + dates sit on one line each, joined by a space: nothing is right-aligned.

LINK_COLOR = HexColor("#1155CC")  # underlined
SEPARATOR = " &nbsp;|&nbsp; "  # the master shows roughly two spaces either side of the bar

_REG, _BOLD, _ITAL, _BOLDITAL = "ResumeArial", "ResumeArial-Bold", "ResumeArial-Italic", "ResumeArial-BoldItalic"


def _register_fonts() -> bool:
    """Register Arial from FONT_DIR. Returns False (and uses Helvetica) if it is missing."""
    files = {
        _REG: "Arial.ttf",
        _BOLD: "Arial Bold.ttf",
        _ITAL: "Arial Italic.ttf",
        _BOLDITAL: "Arial Bold Italic.ttf",
    }
    paths = {name: Path(FONT_DIR) / f for name, f in files.items()}
    if not all(p.exists() for p in paths.values()):
        return False
    try:
        for name, p in paths.items():
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, str(p)))
        pdfmetrics.registerFontFamily(_REG, normal=_REG, bold=_BOLD, italic=_ITAL, boldItalic=_BOLDITAL)
    except Exception:
        return False
    return True


class _Fonts:
    def __init__(self) -> None:
        self.arial = _register_fonts()
        if self.arial:
            self.reg, self.bold, self.ital = _REG, _BOLD, _ITAL
        else:
            self.reg, self.bold, self.ital = "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"
        self.glyph = BULLET_GLYPH if self.arial else BULLET_GLYPH_FALLBACK

    def clean(self, text: str) -> str:
        """Escape for Paragraph markup; in fallback mode swap characters Helvetica cannot draw."""
        if not self.arial:
            text = text.replace(BULLET_GLYPH, BULLET_GLYPH_FALLBACK)
            text = text.encode("cp1252", errors="replace").decode("cp1252")
        return escape(text)


def _styles(f: _Fonts) -> dict[str, ParagraphStyle]:
    def st(name: str, **kw) -> ParagraphStyle:
        base = dict(fontName=f.reg, fontSize=SIZE_BODY, leading=LEADING, spaceBefore=0, spaceAfter=0)
        base.update(kw)
        return ParagraphStyle(name, **base)

    bullet = st(
        "bullet",
        leading=LEADING_BULLET,
        spaceBefore=SPACE_BEFORE_BULLET,
        leftIndent=BULLET_INDENT,
        bulletIndent=0,
        bulletFontName=f.reg,
        bulletFontSize=SIZE_BULLET_GLYPH,
    )
    return {
        "name": st("name", fontName=f.bold, fontSize=SIZE_NAME, leading=LEADING_NAME, alignment=TA_CENTER),
        "contact": st("contact", fontSize=SIZE_CONTACT, leading=LEADING_CONTACT, alignment=TA_CENTER),
        "heading": st("heading", fontName=f.bold, fontSize=SIZE_HEADING, spaceBefore=SPACE_BEFORE_SECTION, keepWithNext=1),
        "body": st("body"),
        "org": st("org", spaceBefore=SPACE_BEFORE_ENTRY, keepWithNext=1),
        "org_first": st("org_first", keepWithNext=1),
        "title": st("title", keepWithNext=1),
        "blurb": st("blurb", spaceBefore=SPACE_BEFORE_BLURB, keepWithNext=1),
        "bullet": bullet,
        "bullet_first": ParagraphStyle("bullet_first", parent=bullet, spaceBefore=SPACE_BEFORE_FIRST_BULLET),
        "bullet_after_blurb": ParagraphStyle(
            "bullet_after_blurb", parent=bullet, spaceBefore=SPACE_BEFORE_BULLET_AFTER_BLURB
        ),
    }


def _runs_markup(runs: list[Run], f: _Fonts) -> str:
    out = []
    for r in runs:
        t = f.clean(r.text)
        if r.bold:
            t = f"<b>{t}</b>"
        if r.italic:
            t = f"<i>{t}</i>"
        out.append(t)
    return "".join(out)


def _story(resume: Resume, f: _Fonts) -> list:
    s = _styles(f)
    h = resume.header
    c = f.clean
    story: list = [Paragraph(c(h.name), s["name"])]

    hex_color = LINK_COLOR.hexval()[2:]
    parts = [c(p) for p in h.contact]
    parts += [f'<a href="{escape(l.url, {chr(34): "&quot;"})}" color="#{hex_color}"><u>{c(l.label)}</u></a>' for l in h.links]
    story.append(Paragraph(SEPARATOR.join(parts), s["contact"]))

    def heading(text: str) -> None:
        story.append(Paragraph(text, s["heading"]))

    def bullet(markup: str, style: str = "bullet") -> None:
        story.append(Paragraph(markup, s[style], bulletText=f.glyph))

    heading("SUMMARY")
    story.append(Paragraph(c(resume.summary), s["body"]))

    heading("EXPERIENCE")
    for n, e in enumerate(resume.experience):
        story.append(Paragraph(f"<b>{c(e.org)}</b> {c(e.location)}".rstrip(), s["org" if n else "org_first"]))
        story.append(Paragraph(f"<i>{c(e.title)}</i> {c(e.dates)}".rstrip(), s["title"]))
        if e.blurb:
            story.append(Paragraph(c(e.blurb), s["blurb"]))
        for i, b in enumerate(e.bullets):
            first = "bullet_after_blurb" if e.blurb else "bullet_first"
            bullet(c(b.text), first if i == 0 else "bullet")

    if resume.publications:
        heading("PUBLICATIONS")
        for pub in resume.publications:
            bullet(_runs_markup(pub, f))

    if resume.skills:
        heading("SKILLS")
        for sk in resume.skills:
            bullet(f"<b>{c(sk.name)}:</b> {c(', '.join(sk.items))}")

    if resume.education:
        heading("EDUCATION")
        for ed in resume.education:
            bullet(f"<b>{c(ed.school)}</b>, {c(ed.detail)} {c(ed.dates)}".rstrip())
    return story


def render_pdf(resume: Resume, out: Path) -> int:
    """Write the PDF to `out` and return its page count."""
    f = _Fonts()
    width, height = PAGE_SIZE
    doc = BaseDocTemplate(
        str(out),
        pagesize=PAGE_SIZE,
        leftMargin=MARGIN_LEFT,
        rightMargin=MARGIN_RIGHT,
        topMargin=MARGIN_TOP,
        bottomMargin=MARGIN_BOTTOM,
        title=resume.header.name,
        author=resume.header.name,
    )
    frame = Frame(
        MARGIN_LEFT, MARGIN_BOTTOM, width - MARGIN_LEFT - MARGIN_RIGHT, height - MARGIN_TOP - MARGIN_BOTTOM,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id="body",
    )
    doc.addPageTemplates([PageTemplate(id="p", frames=[frame])])
    doc.build(_story(resume, f))
    return doc.page


def fit_to_pages(resume: Resume, ranking: list[str], out: Path, max_pages: int = 2) -> tuple[Resume, list[str]]:
    """Cut the lowest-ranked bullets (never an entry's last one) until the PDF fits `max_pages`.

    `ranking` lists bullet IDs best first. Returns the fitted resume and the IDs it cut; the
    fitted PDF is left at `out`.
    """
    current = resume.model_copy(deep=True)
    cut: list[str] = []
    order = list(ranking) + [b for b in current.all_bullets() if b not in ranking]
    while render_pdf(current, out) > max_pages:
        counts = {e.id: len(e.bullets) for e in current.experience}
        owner = {b.id: e.id for e in current.experience for b in e.bullets}
        victim = next((b for b in reversed(order) if b in owner and counts[owner[b]] > 1), None)
        if victim is None:
            break
        for e in current.experience:
            e.bullets = [b for b in e.bullets if b.id != victim]
        cut.append(victim)
    return current, cut
