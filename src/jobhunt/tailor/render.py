"""Render a Resume to PDF in the reference layout: Carlito, ruled ALL-CAPS headings, hanging bullets."""

import re
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.colors import black
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import BaseDocTemplate, Flowable, Frame, PageTemplate, Paragraph

from jobhunt.tailor.model import Resume, Run

# --- Layout constants, measured from the reference PDF (points; y is distance from page top) ---
CARLITO_PATH = Path(
    "/System/Library/PrivateFrameworks/FontServices.framework/Versions/A/Resources/Fonts/"
    "ApplicationSupport/Carlito.ttc"
)
PAGE_SIZE = letter  # 612 x 792
MARGIN_LEFT = MARGIN_RIGHT = 50.5
MARGIN_BOTTOM = 50.0
TOP_FIRST = 39.1  # name baseline (16pt) lands at y=55.1
TOP_LATER = 45.3  # first 10pt baseline on later pages lands at y=55.3

SIZE_NAME = 16.0
SIZE_BODY = 10.0
SIZE_HEADING = 11.0
LEADING = 12.2
LEADING_NAME = 19.8  # name -> contact baseline is 13.8

HEADING_PAD_ABOVE = 7.0  # previous baseline -> heading baseline is 19.2
HEADING_BELOW = 6.3  # heading baseline -> first body baseline is 16.3
HEADING_BELOW_ENTRIES = 8.3  # ... 18.3 before the first experience org line
HEADING_RULE_GAP = 3.0
HEADING_RULE_WIDTH = 0.75
SPACE_BEFORE_ENTRY = 4.0  # last line -> next org baseline is 16.2
BULLET_INDENT = 18.0  # bullet at x=50.5, text (and wrapped lines) at x=68.5
BULLET_GLYPH = "\u2022"

_REG, _BOLD, _ITAL, _BOLDITAL = "Carlito", "Carlito-Bold", "Carlito-Italic", "Carlito-BoldItalic"
_SUBFONTS = {_REG: 0, _ITAL: 1, _BOLDITAL: 2, _BOLD: 3}
_DATE_SEP = re.compile(r"\s*[\u2013\u2014-]\s*")


def _register_fonts() -> bool:
    """Register Carlito from CARLITO_PATH. Returns False (and uses Helvetica) if it is missing."""
    path = Path(CARLITO_PATH)
    if not path.exists():
        return False
    try:
        for name, idx in _SUBFONTS.items():
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, str(path), subfontIndex=idx))
        pdfmetrics.registerFontFamily(_REG, normal=_REG, bold=_BOLD, italic=_ITAL, boldItalic=_BOLDITAL)
    except Exception:
        return False
    return True


class _Fonts:
    def __init__(self) -> None:
        self.carlito = _register_fonts()
        if self.carlito:
            self.reg, self.bold, self.ital = _REG, _BOLD, _ITAL
        else:
            self.reg, self.bold, self.ital = "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"

    def clean(self, text: str) -> str:
        """Escape for Paragraph markup; in fallback mode swap characters Helvetica cannot draw."""
        text = text.replace("\u25cf", BULLET_GLYPH)
        if not self.carlito:
            text = text.encode("cp1252", errors="replace").decode("cp1252")
        return escape(text)


def _dates(text: str) -> str:
    """"Jan 2026 \u2013 Present" -> "Jan 2026 to Present"."""
    return _DATE_SEP.sub(" to ", text.strip())


class _Heading(Flowable):
    """ALL-CAPS bold heading with a full-width rule just below its baseline."""

    def __init__(self, text: str, f: _Fonts, below: float = HEADING_BELOW) -> None:
        super().__init__()
        self.text, self.font, self.below = text.upper(), f.bold, below
        self.keepWithNext = 1
        self.spaceBefore = HEADING_PAD_ABOVE
        self.spaceAfter = 0

    def wrap(self, availWidth, availHeight):
        self.width = availWidth
        self.height = SIZE_BODY + self.below  # baseline sits SIZE_BODY below the top, like a body line
        return availWidth, self.height

    def getSpaceBefore(self):
        return self.spaceBefore

    def getSpaceAfter(self):
        return 0

    def draw(self):
        c = self.canv
        y = self.height - SIZE_BODY
        c.setFillColor(black)
        c.setFont(self.font, SIZE_HEADING)
        c.drawString(0, y, self.text)
        c.setStrokeColor(black)
        c.setLineWidth(HEADING_RULE_WIDTH)
        c.line(0, y - HEADING_RULE_GAP, self.width, y - HEADING_RULE_GAP)


def _styles(f: _Fonts) -> dict[str, ParagraphStyle]:
    def st(name: str, **kw) -> ParagraphStyle:
        base = dict(fontName=f.reg, fontSize=SIZE_BODY, leading=LEADING, spaceBefore=0, spaceAfter=0)
        base.update(kw)
        return ParagraphStyle(name, **base)

    return {
        "name": st("name", fontName=f.bold, fontSize=SIZE_NAME, leading=LEADING_NAME, alignment=TA_CENTER),
        "contact": st("contact", alignment=TA_CENTER),
        "body": st("body"),
        "org": st("org", spaceBefore=SPACE_BEFORE_ENTRY, keepWithNext=1),
        "org_first": st("org_first", keepWithNext=1),
        "title": st("title", keepWithNext=1),
        "blurb": st("blurb", fontName=f.ital),
        "bullet": st("bullet", leftIndent=BULLET_INDENT, bulletIndent=0, bulletFontName=f.reg, bulletFontSize=SIZE_BODY),
    }


def _plain_runs(runs: list[Run], f: _Fonts) -> str:
    return "".join(f.clean(r.text) for r in runs)  # publications are all regular text


def _story(resume: Resume, f: _Fonts) -> list:
    s = _styles(f)
    h = resume.header
    c = f.clean
    story: list = [Paragraph(c(h.name), s["name"])]

    parts = [c(p) for p in h.contact]
    parts += [f'<a href="{escape(l.url, {chr(34): "&quot;"})}" color="black">{c(l.url)}</a>' for l in h.links]
    story.append(Paragraph(" | ".join(parts), s["contact"]))

    def bullet(markup: str) -> None:
        story.append(Paragraph(markup, s["bullet"], bulletText=BULLET_GLYPH))

    if resume.summary:
        story.append(_Heading("Summary", f))
        story.append(Paragraph(c(resume.summary), s["body"]))

    if resume.experience:
        story.append(_Heading("Experience", f, HEADING_BELOW_ENTRIES))
    for n, e in enumerate(resume.experience):
        org = ", ".join(x for x in (e.org, e.location) if x)
        story.append(Paragraph(f"<b>{c(org)}</b>", s["org" if n else "org_first"]))
        title = f"<b>{c(e.title)}</b>"
        if e.dates:
            title += f", {c(_dates(e.dates))}"
        story.append(Paragraph(title, s["title"]))
        if e.blurb:
            story.append(Paragraph(c(e.blurb), s["blurb"]))
        for b in e.bullets:
            bullet(c(b.text))

    if resume.publications:
        story.append(_Heading("Publications", f))
        for pub in resume.publications:
            bullet(_plain_runs(pub, f))

    if resume.skills:
        story.append(_Heading("Skills", f))
        for sk in resume.skills:
            bullet(f"<b>{c(sk.name)}</b>: {c(', '.join(sk.items))}")

    if resume.education:
        story.append(_Heading("Education", f))
        for ed in resume.education:
            line = ", ".join(x for x in (ed.school, ed.detail, _dates(ed.dates)) if x)
            bullet(c(line))
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
        title=resume.header.name,
        author=resume.header.name,
    )

    def frame(top: float, fid: str) -> Frame:
        return Frame(
            MARGIN_LEFT, MARGIN_BOTTOM, width - MARGIN_LEFT - MARGIN_RIGHT, height - top - MARGIN_BOTTOM,
            leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id=fid,
        )

    doc.addPageTemplates([
        PageTemplate(id="first", frames=[frame(TOP_FIRST, "f1")], autoNextPageTemplate="later"),
        PageTemplate(id="later", frames=[frame(TOP_LATER, "f2")]),
    ])
    doc.build(_story(resume, f))
    return doc.page


def fit_to_pages(resume: Resume, ranking: list[str], out: Path, max_pages: int = 2) -> tuple[Resume, list[str], int]:
    """Cut the lowest-ranked bullets (never an entry's last one) until the PDF fits `max_pages`.

    `ranking` lists bullet IDs best first. Returns the fitted resume, the IDs it cut and the final
    page count (above `max_pages` when nothing more could be cut); the fitted PDF is left at `out`.
    """
    current = resume.model_copy(deep=True)
    cut: list[str] = []
    order = list(ranking) + [b for b in current.all_bullets() if b not in ranking]
    while (pages := render_pdf(current, out)) > max_pages:
        counts = {e.id: len(e.bullets) for e in current.experience}
        owner = {b.id: e.id for e in current.experience for b in e.bullets}
        victim = next((b for b in reversed(order) if b in owner and counts[owner[b]] > 1), None)
        if victim is None:
            break
        for e in current.experience:
            e.bullets = [b for b in e.bullets if b.id != victim]
        cut.append(victim)
    return current, cut, pages
