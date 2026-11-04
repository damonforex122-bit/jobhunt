import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Company, Fit, Job, Person
from .urls import normalize_url

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    ats TEXT, slug TEXT, domain TEXT, last_scanned TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    location TEXT, salary_min INTEGER, salary_max INTEGER, description TEXT,
    source TEXT NOT NULL, posted_at TEXT, first_seen TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    flags TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS fits (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    keyword_score INTEGER NOT NULL,
    missing_keywords TEXT NOT NULL DEFAULT '[]',
    llm_score INTEGER, match_pct INTEGER, reasons TEXT, gaps TEXT,
    rewritten_bullets TEXT NOT NULL DEFAULT '[]',
    model TEXT, graded_at TEXT NOT NULL, error TEXT
);
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,
    job_id INTEGER,
    name TEXT NOT NULL,
    title TEXT,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    linkedin_url TEXT,
    confidence REAL NOT NULL,
    UNIQUE(company, name)
);
"""

_JOB_UPDATE_FIELDS = (
    "company", "title", "location", "salary_min", "salary_max",
    "description", "source", "posted_at",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# --- companies -------------------------------------------------------------

def upsert_company(conn: sqlite3.Connection, c: Company) -> int:
    conn.execute(
        """INSERT INTO companies (name, ats, slug, domain, last_scanned, last_error)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
             ats = COALESCE(excluded.ats, ats),
             slug = COALESCE(excluded.slug, slug),
             domain = COALESCE(excluded.domain, domain),
             last_scanned = COALESCE(excluded.last_scanned, last_scanned),
             last_error = COALESCE(excluded.last_error, last_error)""",
        (c.name, c.ats, c.slug, c.domain, c.last_scanned, c.last_error),
    )
    conn.commit()
    return conn.execute("SELECT id FROM companies WHERE name = ?", (c.name,)).fetchone()[0]


def list_companies(conn: sqlite3.Connection) -> list[Company]:
    rows = conn.execute("SELECT * FROM companies ORDER BY name").fetchall()
    return [Company(**dict(r)) for r in rows]


def get_company(conn: sqlite3.Connection, name: str) -> Company | None:
    row = conn.execute("SELECT * FROM companies WHERE name = ?", (name,)).fetchone()
    return Company(**dict(row)) if row else None


def set_company_scan(conn: sqlite3.Connection, name: str, *, error: str | None) -> None:
    conn.execute(
        "UPDATE companies SET last_scanned = ?, last_error = ? WHERE name = ?",
        (_now(), error, name),
    )
    conn.commit()


# --- jobs ------------------------------------------------------------------

def _job_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["flags"] = json.loads(d["flags"] or "[]")
    return d


def upsert_job(conn: sqlite3.Connection, j: Job) -> tuple[int, bool]:
    url = normalize_url(j.url)
    existing = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    if existing is None:
        cur = conn.execute(
            """INSERT INTO jobs (company, title, url, location, salary_min, salary_max,
                   description, source, posted_at, first_seen, status, flags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (j.company, j.title, url, j.location, j.salary_min, j.salary_max,
             j.description, j.source, j.posted_at, j.first_seen or _now(),
             j.status, json.dumps(j.flags)),
        )
        conn.commit()
        return cur.lastrowid, True

    updates = {f: getattr(j, f) for f in _JOB_UPDATE_FIELDS if getattr(j, f) is not None}
    if j.flags:
        updates["flags"] = json.dumps(j.flags)
    if existing["status"] == "new":
        updates["status"] = j.status
    sets = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*updates.values(), existing["id"]))
    conn.commit()
    return existing["id"], False


def get_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    return _job_row(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def list_jobs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    min_score: int | None = None,
    company: str | None = None,
) -> list[dict]:
    where, args = [], []
    if status is not None:
        where.append("j.status = ?")
        args.append(status)
    if min_score is not None:
        where.append("f.llm_score >= ?")
        args.append(min_score)
    if company is not None:
        where.append("j.company = ?")
        args.append(company)
    sql = (
        "SELECT j.*, f.keyword_score, f.llm_score, f.match_pct, f.error AS fit_error "
        "FROM jobs j LEFT JOIN fits f ON f.job_id = j.id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY f.llm_score DESC, j.id DESC"
    return [_job_row(r) for r in conn.execute(sql, args).fetchall()]


# --- fits ------------------------------------------------------------------

def save_fit(conn: sqlite3.Connection, f: Fit) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO fits (job_id, keyword_score, missing_keywords, llm_score,
               match_pct, reasons, gaps, rewritten_bullets, model, graded_at, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (f.job_id, f.keyword_score, json.dumps(f.missing_keywords), f.llm_score,
         f.match_pct, f.reasons, f.gaps, json.dumps(f.rewritten_bullets),
         f.model, f.graded_at, f.error),
    )
    conn.commit()


def get_fit(conn: sqlite3.Connection, job_id: int) -> Fit | None:
    row = conn.execute("SELECT * FROM fits WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["missing_keywords"] = json.loads(d["missing_keywords"])
    d["rewritten_bullets"] = json.loads(d["rewritten_bullets"])
    return Fit(**d)


# --- people ----------------------------------------------------------------

def upsert_person(conn: sqlite3.Connection, p: Person) -> int:
    conn.execute(
        """INSERT INTO people (company, job_id, name, title, source, source_url,
               linkedin_url, confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(company, name) DO UPDATE SET
             job_id = COALESCE(excluded.job_id, job_id),
             title = COALESCE(excluded.title, title),
             source = excluded.source,
             source_url = excluded.source_url,
             linkedin_url = COALESCE(excluded.linkedin_url, linkedin_url),
             confidence = excluded.confidence""",
        (p.company, p.job_id, p.name, p.title, p.source, p.source_url,
         p.linkedin_url, p.confidence),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM people WHERE company = ? AND name = ?", (p.company, p.name)
    ).fetchone()[0]


def list_people(conn: sqlite3.Connection, company: str) -> list[Person]:
    rows = conn.execute(
        "SELECT * FROM people WHERE company = ? ORDER BY confidence DESC, name", (company,)
    ).fetchall()
    return [Person(**dict(r)) for r in rows]


def set_job_status(conn: sqlite3.Connection, job_id: int, status: str) -> None:
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    conn.commit()
