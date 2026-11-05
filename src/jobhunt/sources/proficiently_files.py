"""Import the files Proficiently leaves beside job history: materials, warm intros,
careers pages and digests. Strictly read-only on the source directory."""
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from ..db import get_company, save_material, upsert_company, upsert_person
from ..models import Company, Person
from ..urls import ats_from_url, normalize_url

KNOWN_FILES = {
    "resume.pdf": "resume_pdf",
    "resume.md": "resume_md",
    "resume.docx": "resume_docx",
    "cover-letter.md": "cover_letter",
    "apply-sheet.md": "apply_sheet",
    "warm-intros.md": "warm_intros",
    "posting.md": "posting",
}
URL_RE = re.compile(r"https?://[^\s)>\]|]+")
LINKEDIN_RE = re.compile(r"https?://(?:[\w-]+\.)?linkedin\.com/in/[^\s)>\]|]+")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _find_job_id(conn: sqlite3.Connection, folder: Path) -> int | None:
    posting = folder / "posting.md"
    if posting.is_file():
        m = URL_RE.search(posting.read_text(errors="replace"))
        if m:
            row = conn.execute("SELECT id FROM jobs WHERE url = ?",
                               (normalize_url(m.group(0)),)).fetchone()
            if row:
                return row["id"]
    best: tuple[str, int] | None = None
    for row in conn.execute("SELECT id, company, title FROM jobs"):
        prefix = f"{_slugify(row['company'])}-{_slugify(row['title'])}-"
        name = folder.name
        if name.startswith(prefix) and DATE_RE.fullmatch(name[len(prefix):]):
            if best is None or name[len(prefix):] > best[0]:
                best = (name[len(prefix):], row["id"])
    return best[1] if best else None


def import_materials(conn: sqlite3.Connection, root: Path) -> dict:
    root = Path(root)
    real_root = root.resolve()
    jobs_root = root / "jobs"
    counts = {"folders": 0, "matched": 0, "unmatched_folders": 0, "files": 0}
    if not jobs_root.is_dir():
        return counts
    for folder in sorted(d for d in jobs_root.iterdir() if d.is_dir()):
        counts["folders"] += 1
        job_id = _find_job_id(conn, folder)
        if job_id is None:
            counts["unmatched_folders"] += 1
            continue
        counts["matched"] += 1
        job = conn.execute("SELECT company FROM jobs WHERE id = ?", (job_id,)).fetchone()
        for filename, kind in KNOWN_FILES.items():
            p = folder / filename
            if not p.is_file():  # follows symlinks; skips missing files and directories
                continue
            real = p.resolve()
            if not real.is_relative_to(real_root):
                continue
            mtime = datetime.fromtimestamp(real.stat().st_mtime).isoformat(timespec="seconds")
            save_material(conn, job_id, folder.name, kind, str(real), mtime)
            counts["files"] += 1
            if kind == "warm_intros":
                for person in parse_warm_intros(real.read_text(errors="replace"), job["company"]):
                    upsert_person(conn, person.model_copy(update={"job_id": job_id}))
    return counts


def _person(company: str, name: str, title: str | None, linkedin: str | None) -> Person | None:
    name = name.strip(" *_`")
    if not re.search(r"[A-Za-z]", name) or len(name) > 80:
        return None
    return Person(
        company=company, name=name, title=(title or "").strip(" ,;:-—–*") or None,
        source="proficiently", kind="warm_intro",
        source_url=linkedin or f"proficiently:{company}:{name.lower()}",
        linkedin_url=linkedin, confidence=1.0)


def parse_warm_intros(text: str, company: str) -> list[Person]:
    found: dict[str, Person] = {}

    def add(person: Person | None) -> None:
        if person and person.name.lower() not in found:
            found[person.name.lower()] = person

    columns: dict[str, int] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = _split_row(stripped)
            low = [c.lower() for c in cells]
            if columns is None:
                if "name" in low:
                    columns = {c: i for i, c in enumerate(low)}
                continue
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            def cell(*names: str) -> str:
                for n in names:
                    i = columns.get(n)
                    if i is not None and i < len(cells):
                        return cells[i]
                return ""
            row_text = " ".join(cells)
            m = LINKEDIN_RE.search(row_text)
            add(_person(company, re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell("name")),
                        cell("title", "role", "position"), m.group(0) if m else None))
            continue
        columns = None
        if not re.match(r"^[-*]\s+", stripped):
            continue
        body = re.sub(r"^[-*]\s+", "", stripped)
        m = re.match(r"\*\*(?P<n>[^*]+)\*\*|\[(?P<n2>[^\]]+)\]\([^)]*\)", body)
        if not m:
            continue
        name = m.group("n") or m.group("n2")
        rest = body[m.end():]
        li = LINKEDIN_RE.search(body)
        rest = re.sub(r"\[[^\]]*\]\([^)]*\)", "", rest)
        rest = URL_RE.sub("", rest)
        rest = re.sub(r"\([^)]*\)", "", rest)
        title = re.split(r"\s[—–-]\s|:", rest.strip(" ,:—–-"))[0]
        add(_person(company, name, title, li.group(0) if li else None))
    return list(found.values())


def apply_sheet_needs(text: str) -> list[str]:
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().upper() == "NEEDS GEORGIA"
            continue
        if inside and re.match(r"^\s*[-*]\s+", line):
            out.append(line.strip())
    return out


def import_careers(conn: sqlite3.Connection, root: Path) -> dict:
    counts = {"added": 0, "conflicts": 0, "skipped": 0}
    path = Path(root) / "company-careers.json"
    if not path.is_file():
        return counts
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return counts
    if not isinstance(data, dict):
        return counts
    for company, info in data.items():
        url = info.get("careers_url") if isinstance(info, dict) else None
        ats = ats_from_url(url) if isinstance(url, str) and url.startswith("http") else None
        if ats is None:
            counts["skipped"] += 1
            continue
        existing = get_company(conn, company)
        if existing and existing.ats:
            if (existing.ats, existing.slug) != ats:
                counts["conflicts"] += 1
            else:
                counts["skipped"] += 1
            continue
        upsert_company(conn, Company(name=company, ats=ats[0], slug=ats[1]))
        counts["added"] += 1
    return counts


def import_digests(conn: sqlite3.Connection, root: Path) -> int:
    folder = Path(root) / "daily-digest"
    if not folder.is_dir():
        return 0
    added = 0
    for f in sorted(folder.glob("*.md")):
        if not f.is_file() or not DATE_RE.fullmatch(f.stem):
            continue
        real = f.resolve()
        if not real.is_relative_to(Path(root).resolve()):
            continue
        if conn.execute("SELECT 1 FROM runs WHERE kind = 'digest' AND path = ?",
                        (str(real),)).fetchone():
            continue
        text = real.read_text(errors="replace")
        found = high = rows = 0
        m = re.search(r"New jobs found:\s*(\d+)", text)
        score_col: int | None = None
        for line in text.splitlines():
            if not line.lstrip().startswith("|"):
                score_col = None
                continue
            cells = _split_row(line)
            low = [c.lower() for c in cells]
            if score_col is None:
                score_col = low.index("score") if "score" in low else None
                continue
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            rows += 1
            if score_col < len(cells) and cells[score_col].isdigit() and int(cells[score_col]) >= 8:
                high += 1
        found = int(m.group(1)) if m else rows
        stamp = datetime.fromisoformat(f.stem).astimezone().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO runs (kind, started_at, finished_at, counts_json, path) VALUES ('digest', ?, ?, ?, ?)",
            (stamp, stamp, json.dumps({"found": found, "high": high}), str(real)))
        conn.commit()
        added += 1
    return added
