"""Read the master resume PDF back into a structured Resume, check it, and cache it by file hash."""

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

from jobhunt import db
from jobhunt.config import short_error
from jobhunt.tailor.model import (
    Bullet,
    EducationLine,
    Entry,
    Header,
    Link,
    Resume,
    Run,
    SkillCategory,
)

BULLET_GLYPHS = ("●", "•")
LINE_TOLERANCE = 2.5  # points of baseline drift that still count as the same line
INDENT_SLACK = 8.0  # a line starting this far right of the left margin is a hanging-indent continuation

SECTION_KEYS = (  # heading keyword -> section name
    ("SUMMARY", "summary"), ("PROFILE", "summary"), ("EXPERIENCE", "experience"),
    ("PUBLICATION", "publications"), ("SKILL", "skills"), ("EDUCATION", "education"),
)


class MasterError(Exception):
    pass


@dataclass
class _Piece:
    text: str
    bold: bool
    italic: bool


@dataclass
class _Line:
    page: int
    x: float
    y: float
    pieces: list[_Piece] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join("".join(p.text for p in self.pieces).split())

    @property
    def first(self) -> _Piece | None:
        return next((p for p in self.pieces if p.text.strip()), None)


def _lines(reader: PdfReader) -> list[_Line]:
    """Positioned text runs -> lines (new line when the baseline moves or the page changes)."""
    lines: list[_Line] = []
    for pno, page in enumerate(reader.pages):
        def visit(text, cm, tm, font, size, pno=pno):
            text = text[:-1] if text.endswith("\n") else text
            if not text or text == "\n":
                return
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            name = str(font.get("/BaseFont", "")) if font else ""
            piece = _Piece(text, "Bold" in name, "Italic" in name or "Oblique" in name)
            if lines and lines[-1].page == pno and abs(lines[-1].y - y) <= LINE_TOLERANCE:
                lines[-1].pieces.append(piece)
            elif text.strip():
                lines.append(_Line(pno, x, y, [piece]))

        page.extract_text(visitor_text=visit)
    return [ln for ln in lines if ln.text]


def _pdf_text(pdf: Path) -> str:
    return "\n".join(p.extract_text() or "" for p in PdfReader(pdf).pages)


def _section_of(line: _Line) -> str | None:
    t = line.text
    first = line.first
    if not (first and first.bold and len(t) >= 3 and t == t.upper() and any(c.isalpha() for c in t)):
        return None
    for key, name in SECTION_KEYS:
        if key in t:
            return name
    raise MasterError(f"unknown section heading {t!r}")


def _is_bullet(line: _Line) -> bool:
    return line.text.startswith(BULLET_GLYPHS)


def _bullet_runs(first: _Line, rest: list[_Line]) -> list[Run]:
    """Merge a bullet's pieces (minus the glyph) into runs of equal style, joining wrapped lines."""
    runs: list[Run] = []
    for i, ln in enumerate([first, *rest]):
        pieces = list(ln.pieces)
        if i == 0:  # drop the glyph (and the space after it)
            while pieces and not pieces[0].text.strip().strip("".join(BULLET_GLYPHS)):
                pieces.pop(0)
            if pieces:
                pieces[0] = _Piece(pieces[0].text.lstrip().lstrip("".join(BULLET_GLYPHS)).lstrip(),
                                   pieces[0].bold, pieces[0].italic)
        if runs and pieces and not runs[-1].text.endswith(" ") and not pieces[0].text.startswith(" "):
            runs[-1] = runs[-1].model_copy(update={"text": runs[-1].text + " "})
        for p in pieces:
            if runs and runs[-1].bold == p.bold and runs[-1].italic == p.italic:
                runs[-1] = runs[-1].model_copy(update={"text": runs[-1].text + p.text})
            else:
                runs.append(Run(text=p.text, bold=p.bold, italic=p.italic))
    if runs:
        runs[0] = runs[0].model_copy(update={"text": runs[0].text.lstrip()})
        runs[-1] = runs[-1].model_copy(update={"text": runs[-1].text.rstrip()})
    return [r for r in runs if r.text]


def _plain(runs: list[Run]) -> str:
    return " ".join("".join(r.text for r in runs).split())


def _group_bullets(lines: list[_Line], margin: float) -> list[tuple[_Line, list[_Line]]]:
    """Each bullet line with its hanging-indent continuation lines."""
    out: list[tuple[_Line, list[_Line]]] = []
    for ln in lines:
        if _is_bullet(ln):
            out.append((ln, []))
        elif out:
            out[-1][1].append(ln)  # wrapped text, whether or not it kept the hanging indent
        else:
            raise MasterError(f"text before first bullet: {ln.text[:30]!r}")
    return out


def _split_top_level(text: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    parts.append(cur.strip())
    return [p for p in parts if p]


_DATE = r"(?:[A-Za-z]{3,9}\.? )?\d{4}"
_DATES_AT_END = re.compile(rf"(?:^|\s)({_DATE}(?:\s*[–—-]\s*(?:{_DATE}|Present|Current))?)\s*$")


def _education(first: _Line, rest: list[_Line]) -> EducationLine:
    runs = _bullet_runs(first, rest)
    school = "".join(r.text for r in runs if r.bold).strip()
    text = _plain(runs)
    if school and text.startswith(school):
        tail = text[len(school):].lstrip(", ").strip()
    else:
        school, _, tail = text.partition(",")
        school, tail = school.strip(), tail.strip()
    m = _DATES_AT_END.search(tail)
    dates = m.group(1) if m else ""
    detail = tail[: m.start()].strip() if m else tail
    return EducationLine(school=school, detail=detail, dates=dates)


def _skills(first: _Line, rest: list[_Line], n: int) -> SkillCategory:
    text = _plain(_bullet_runs(first, rest))
    name, sep, items = text.partition(":")
    if not sep:
        raise MasterError(f"skill line without a category: {text[:30]!r}")
    return SkillCategory(id=f"s{n}", name=name.strip(), items=_split_top_level(items))


def _experience(lines: list[_Line], margin: float) -> list[Entry]:
    entries: list[Entry] = []
    last = ""  # org | title | blurb | bullet
    cur: Entry | None = None
    bullet_parts: list[tuple[_Line, list[_Line]]] = []

    def flush() -> None:
        nonlocal bullet_parts
        if cur is not None:
            for k, (b, cont) in enumerate(bullet_parts):
                cur.bullets.append(Bullet(id=f"{cur.id}b{k}", text=_plain(_bullet_runs(b, cont))))
        bullet_parts = []

    for ln in lines:
        first = ln.first
        if _is_bullet(ln):
            if cur is None:
                raise MasterError("bullet before any experience entry")
            bullet_parts.append((ln, []))
            last = "bullet"
        elif last == "bullet" and ln.x > margin + INDENT_SLACK:
            bullet_parts[-1][1].append(ln)
        elif first and first.bold:
            flush()
            org = "".join(p.text for p in ln.pieces if p.bold).strip()
            loc = "".join(p.text for p in ln.pieces if not p.bold).strip()
            cur = Entry(id=f"e{len(entries)}", org=org, location=loc, title="", dates="", bullets=[])
            entries.append(cur)
            last = "org"
        elif cur is not None and last == "org" and first and first.italic:
            cur.title = "".join(p.text for p in ln.pieces if p.italic).strip()
            cur.dates = "".join(p.text for p in ln.pieces if not p.italic).strip()
            last = "title"
        elif cur is not None and last in ("title", "blurb"):
            cur.blurb = (cur.blurb + " " + ln.text).strip()
            last = "blurb"
        else:
            raise MasterError(f"unexpected line in experience: {ln.text[:30]!r}")
    flush()
    return entries


def _header(lines: list[_Line], reader: PdfReader) -> Header:
    if len(lines) < 2:
        raise MasterError("header not found")
    name = lines[0].text
    contact_text = " ".join(ln.text for ln in lines[1:])
    parts = [p.strip() for p in contact_text.split("|") if p.strip()]
    contact_line = lines[1]
    urls: list[tuple[float, str]] = []
    for annot in reader.pages[contact_line.page].get("/Annots", []) or []:
        a = annot.get_object()
        uri = (a.get("/A") or {}).get("/URI")
        if uri is None or a.get("/Subtype") != "/Link":
            continue
        x0, y0, x1, y1 = (float(v) for v in a["/Rect"])
        if abs((y0 + y1) / 2 - contact_line.y) < 15:
            urls.append((x0, str(uri)))
    deduped: list[str] = []
    for _, url in sorted(urls):
        if not deduped or deduped[-1] != url:
            deduped.append(url)
    n = len(deduped)
    if n > len(parts):
        raise MasterError("more header links than header parts")
    plain = parts[: len(parts) - n]
    labels = parts[len(parts) - n:]
    return Header(name=name, contact=plain, links=[Link(label=lb, url=u) for lb, u in zip(labels, deduped)])


def parse_master(pdf: Path) -> Resume:
    reader = PdfReader(pdf)
    lines = _lines(reader)
    if not lines:
        raise MasterError("no text found in the PDF")
    margin = min(ln.x for ln in lines)

    header_lines: list[_Line] = []
    sections: dict[str, list[_Line]] = {}
    current: list[_Line] | None = None
    for ln in lines:
        section = _section_of(ln)
        if section:
            if section in sections:
                raise MasterError(f"section {section} appears twice")
            current = sections.setdefault(section, [])
        elif current is None:
            header_lines.append(ln)
        else:
            current.append(ln)
    for needed in ("summary", "experience"):
        if needed not in sections:
            raise MasterError(f"no {needed} section")

    pubs = [_bullet_runs(b, c) for b, c in _group_bullets(sections.get("publications", []), margin)]
    skills = [_skills(b, c, i) for i, (b, c) in enumerate(_group_bullets(sections.get("skills", []), margin))]
    education = [_education(b, c) for b, c in _group_bullets(sections.get("education", []), margin)]
    return Resume(
        header=_header(header_lines, reader),
        summary=" ".join(ln.text for ln in sections["summary"]),
        experience=_experience(sections["experience"], margin),
        publications=pubs,
        skills=skills,
        education=education,
    )


def _squash(text: str) -> str:
    return "".join(text.split())


def verify(resume: Resume, pdf_text: str) -> list[str]:
    """Strings of the parsed resume that don't appear in the PDF's own text (whitespace ignored)."""
    haystack = _squash(pdf_text)
    h = resume.header
    strings: list[str] = [h.name, *h.contact, *(link.label for link in h.links), resume.summary]
    for e in resume.experience:
        strings += [e.org, e.location, e.title, e.dates, e.blurb, *(b.text for b in e.bullets)]
    strings += ["".join(r.text for r in pub) for pub in resume.publications]
    for s in resume.skills:
        strings += [s.name, *s.items]
    for ed in resume.education:
        strings += [ed.school, ed.detail, ed.dates]
    return [s for s in strings if s and _squash(s) not in haystack]


def load_master(conn: sqlite3.Connection, path: Path) -> Resume:
    """The parsed master, from the cache when the file is unchanged. Raises MasterError."""
    path = Path(path)
    if not path.is_file():
        raise MasterError(f"Master resume not found: {path}")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    cached = db.get_master_cache(conn, sha)
    if cached:
        return Resume.model_validate_json(cached)
    try:
        resume = parse_master(path)
        missing = verify(resume, _pdf_text(path))
    except Exception as e:
        raise MasterError(f"Couldn't read the master resume reliably: {short_error(e)}") from e
    if missing:
        shown = "; ".join(repr(m[:40]) for m in missing[:3])
        raise MasterError(short_error(Exception(
            f"Couldn't read the master resume reliably: {len(missing)} parsed strings are not in the PDF, e.g. {shown}")))
    db.save_master_cache(conn, sha, resume.model_dump_json())
    return resume
