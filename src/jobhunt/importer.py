"""Import existing `.proficiently` data. Strictly read-only on the source directory."""
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .db import save_fit, upsert_company, upsert_job, upsert_person
from .models import Company, Fit, Job, Person
from .urls import ats_from_url

REQUIRED_COLUMNS = {"company", "role", "score", "job url"}
PERSON_RE = re.compile(r"^\s*-\s+\*\*(?P<name>[^*]+)\*\*\s*,?\s*(?P<rest>.*?):\s*(?P<url>https?://\S+)\s*$")


@dataclass
class ImportedJob:
    job: Job
    score: int | None
    reason: str | None


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
            reason = "|".join(cells[ri:]).replace("\\|", "|").strip() or None
        job = Job(
            company=company,
            title=title,
            url=url,
            source="import",
            status=cell("status") or "new",
            first_seen=cell("date") or None,
        )
        results.append(ImportedJob(job=job, score=score, reason=reason))
    return results


def parse_job_dir(path: Path) -> tuple[str | None, list[Person]]:
    path = Path(path)
    posting_file = path / "posting.md"
    posting = posting_file.read_text() if posting_file.is_file() else None
    people: list[Person] = []
    people_file = path / "people.md"
    if people_file.is_file():
        for line in people_file.read_text().splitlines():
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


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _find_job_dir(dirs: list[Path], job: Job) -> Path | None:
    prefix = f"{slugify(job.company)}-{slugify(job.title)}-"
    matches = [d for d in dirs if d.name.startswith(prefix)]
    return matches[-1] if matches else None  # dir names end in a date: latest wins


def run_import(conn: sqlite3.Connection, proficiently_dir: Path) -> dict[str, int]:
    root = Path(proficiently_dir)
    history = root / "job-history.md"
    imported = parse_job_history(history.read_text()) if history.is_file() else []
    jobs_root = root / "jobs"
    dirs = sorted(d for d in jobs_root.iterdir() if d.is_dir()) if jobs_root.is_dir() else []

    counts = {"jobs": 0, "new": 0, "companies": 0, "people": 0}
    companies: set[str] = set()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for item in imported:
        job = item.job
        ats = ats_from_url(job.url)
        upsert_company(
            conn,
            Company(name=job.company, ats=ats[0] if ats else None, slug=ats[1] if ats else None),
        )
        companies.add(job.company)

        job_dir = _find_job_dir(dirs, job)
        people: list[Person] = []
        if job_dir is not None:
            posting, people = parse_job_dir(job_dir)
            if posting:
                job = job.model_copy(update={"description": posting})

        job_id, is_new = upsert_job(conn, job)
        counts["jobs"] += 1
        counts["new"] += int(is_new)
        save_fit(
            conn,
            Fit(
                job_id=job_id,
                keyword_score=0,
                llm_score=item.score,
                reasons=item.reason,
                model="proficiently-import",
                graded_at=now,
            ),
        )
        for person in people:
            upsert_person(conn, person.model_copy(update={"company": job.company, "job_id": job_id}))
            counts["people"] += 1

    counts["companies"] = len(companies)
    return counts


def register(sub) -> None:
    from .config import load_settings

    p = sub.add_parser("import", help="Import existing .proficiently job history")
    p.add_argument("--dir", type=Path, default=None, help="Path to the .proficiently directory")

    def handler(args) -> int:
        from .db import connect

        settings = load_settings()
        directory = Path(args.dir).expanduser() if args.dir else settings.proficiently_dir
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(settings.db_path)
        try:
            print(run_import(conn, directory))
        finally:
            conn.close()
        return 0

    p.set_defaults(handler=handler)
