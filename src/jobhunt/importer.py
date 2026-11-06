"""Import existing `.proficiently` data. Strictly read-only on the source directory."""
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .db import finish_run, set_job_status, start_run, upsert_company, upsert_job, upsert_person
from .models import Company, Job, Person
from .urls import ats_from_url, normalize_url, slugify

TRIAGE = {"ready", "needs", "low"}
FORWARD = {"applied", "screening", "interviewing", "offer"}
OPEN_STATUSES = ("new", "ready", "needs", "low")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
REQUIRED_COLUMNS = {"company", "job url"}
PERSON_RE = re.compile(r"^\s*-\s+\*\*(?P<name>[^*]+)\*\*\s*,?\s*(?P<rest>.*?):\s*(?P<url>https?://\S+)\s*$")


class ImportSourceError(FileNotFoundError):
    """The Proficiently directory or its job-history.md is missing."""


@dataclass
class ImportedJob:
    job: Job
    score: int | None
    reason: str | None
    status_cell: str = ""
    date: str = ""


def map_status(cell: str) -> tuple[str, str] | None:
    s = (cell or "").strip().lower()
    if s in TRIAGE:
        return ("triage", s)
    if s in FORWARD:
        return ("forward", s)
    return None


def _split_row(line: str) -> list[str]:
    line = line.strip().strip("|")
    return [c.strip() for c in re.split(r"(?<!\\)\|", line)]


def parse_job_history(text: str) -> list[ImportedJob]:
    results: list[ImportedJob] = []
    columns: dict[str, int] | None = None
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            columns = None  # a table ends at the first non-table line
            continue
        cells = _split_row(line)
        lowered = [c.lower() for c in cells]
        if columns is None:
            if REQUIRED_COLUMNS <= set(lowered):
                columns = {name: i for i, name in enumerate(lowered)}
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue  # separator row

        def cell(name: str) -> str:
            i = columns.get(name)
            return cells[i] if i is not None and i < len(cells) else ""

        company, title, url = cell("company"), cell("role"), cell("job url")
        if not (company and title and url.startswith("http")):
            continue
        score_text = cell("score")
        score = int(score_text) if score_text.isdigit() else None
        reason = None
        ri = columns.get("reason")
        if ri is not None and ri < len(cells):
            extra = max(0, len(cells) - len(columns))  # cells split off by unescaped pipes inside the reason
            reason = "|".join(cells[ri : ri + 1 + extra]).replace("\\|", "|").strip() or None
        job = Job(
            company=company,
            title=title,
            url=url,
            source="proficiently",
            status="new",  # Proficiently's status is applied by run_import, never via upsert_job
            first_seen=cell("date") or None,
        )
        results.append(ImportedJob(job=job, score=score, reason=reason,
                                   status_cell=cell("status"), date=cell("date")))
    return results


def _read_inside(file: Path, root: Path) -> str | None:
    """File text, or None when missing or resolving outside `root` (e.g. a symlink)."""
    if not file.is_file() or not file.resolve().is_relative_to(root.resolve()):
        return None
    return file.read_text(errors="replace")


def parse_job_dir(path: Path, root: Path | None = None) -> tuple[str | None, list[Person]]:
    path = Path(path)
    root = Path(root) if root is not None else path
    posting = _read_inside(path / "posting.md", root)
    people: list[Person] = []
    people_text = _read_inside(path / "people.md", root)
    if people_text is not None:
        for line in people_text.splitlines():
            m = PERSON_RE.match(line)
            if not m:
                continue
            title = re.sub(r"\s*\([^)]*\)\s*$", "", m["rest"]).strip(" ,") or None
            people.append(
                Person(
                    company="",  # filled in by run_import
                    name=m["name"].strip(),
                    title=title,
                    source="exa",
                    source_url=m["url"],
                    confidence=0.5,
                )
            )
    return posting, people


def _find_job_dir(dirs: list[Path], job: Job) -> Path | None:
    prefix = f"{slugify(job.company)}-{slugify(job.title)}-"
    matches = [
        d for d in dirs
        if d.name.startswith(prefix) and DATE_RE.fullmatch(d.name[len(prefix):])
    ]
    return max(matches, key=lambda d: d.name) if matches else None  # latest date wins


def run_import(conn: sqlite3.Connection, proficiently_dir: Path) -> dict:
    root = Path(proficiently_dir)
    counts = {"jobs": 0, "new": 0, "updated": 0, "moved": 0, "unknown_status": 0,
              "companies": 0, "people": 0}
    companies: set[str] = set()
    run_id = start_run(conn, "import", str(root))
    try:
        history = root / "job-history.md"
        denied = (f"Can't read {root}: permission denied (macOS may block background access to "
                  "~/Documents — see README)")
        try:
            if not root.is_dir():
                raise ImportSourceError(f"Proficiently directory not found: {root} (set PROFICIENTLY_DIR)")
            if not history.is_file():
                raise ImportSourceError(f"job-history.md not found in {root}")
            rows = parse_job_history(history.read_text())
            extra = root / "_history-add.md"
            if extra.is_file():
                rows += parse_job_history(extra.read_text())
        except PermissionError as e:
            raise ImportSourceError(denied) from e
        by_url: dict[str, ImportedJob] = {}
        for item in rows:  # document order: a later row for the same URL wins
            by_url[normalize_url(item.job.url)] = item
        jobs_root = root / "jobs"
        try:
            dirs = sorted(d for d in jobs_root.iterdir() if d.is_dir()) if jobs_root.is_dir() else []
        except PermissionError as e:
            raise ImportSourceError(denied) from e

        for url, item in by_url.items():
            job = item.job
            ats = ats_from_url(job.url)
            upsert_company(
                conn,
                Company(name=job.company, ats=ats[0] if ats else None, slug=ats[1] if ats else None),
            )
            companies.add(job.company)

            job_dir = _find_job_dir(dirs, job)
            people: list[Person] = []
            posting = None
            if job_dir is not None:
                posting, people = parse_job_dir(job_dir, root)
            existing = conn.execute("SELECT id, description FROM jobs WHERE url = ?", (url,)).fetchone()
            if existing is None:
                if posting:
                    job = job.model_copy(update={"description": posting})
                job_id, is_new = upsert_job(conn, job)
            else:  # a re-import never rewrites title/company/etc; it only fills an empty description
                job_id, is_new = existing["id"], False
                if posting and not (existing["description"] or "").strip():
                    conn.execute("UPDATE jobs SET description = ? WHERE id = ?", (posting, job_id))
                    conn.commit()
            counts["jobs"] += 1
            counts["new"] += int(is_new)
            cur = conn.execute("SELECT status, prof_score, prof_reason FROM jobs WHERE id = ?",
                               (job_id,)).fetchone()
            changed = False
            if (cur["prof_score"], cur["prof_reason"]) != (item.score, item.reason):
                conn.execute("UPDATE jobs SET prof_score = ?, prof_reason = ? WHERE id = ?",
                             (item.score, item.reason, job_id))
                conn.commit()
                changed = True

            mapped = map_status(item.status_cell)
            if mapped is None:
                if item.status_cell.strip():
                    counts["unknown_status"] += 1
            else:
                kind, target = mapped
                if kind == "triage" and cur["status"] == "new":
                    set_job_status(conn, job_id, target, source="proficiently")
                    counts["moved"] += 1
                elif kind == "forward" and cur["status"] in OPEN_STATUSES:
                    set_job_status(conn, job_id, target, f"From Proficiently ({item.date})" if item.date else "From Proficiently",
                                   source="proficiently")
                    counts["moved"] += 1
            if changed and not is_new:
                counts["updated"] += 1
            for person in people:
                upsert_person(conn, person.model_copy(update={"company": job.company, "job_id": job_id}))
                counts["people"] += 1
        counts["companies"] = len(companies)
        from .sources.proficiently_files import import_careers, import_digests, import_materials

        counts["materials"] = import_materials(conn, root)
        counts["careers"] = import_careers(conn, root)
        digest_stats: dict = {}
        counts["digests"] = import_digests(conn, root, digest_stats)
        counts["digests_skipped"] = digest_stats.get("skipped", 0)
    except Exception as e:
        finish_run(conn, run_id, counts, error=str(e))
        raise
    finish_run(conn, run_id, counts)
    return counts


def register(sub) -> None:
    from .config import load_settings, short_error

    p = sub.add_parser("import", help="Import existing .proficiently job history")
    p.add_argument("--dir", type=Path, default=None, help="Path to the .proficiently directory")

    def handler(args) -> int:
        from .db import connect

        settings = load_settings()
        directory = Path(args.dir).expanduser() if args.dir else settings.proficiently_dir
        if directory is None:
            print("No Proficiently folder set (PROFICIENTLY_DIR).")
            return 1
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(settings.db_path)
        try:
            print(run_import(conn, directory))
        except (ImportSourceError, OSError) as e:
            print(f"error: {short_error(e)}")
            return 1
        except Exception as e:
            print(f"error: import failed: {short_error(e)}")
            return 1
        finally:
            conn.close()
        return 0

    p.set_defaults(handler=handler)
