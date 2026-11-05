import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from .models import CLOSED, PIPELINE, Company, Fit, Job, Person
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
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    status TEXT NOT NULL,
    note TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id);
CREATE TABLE IF NOT EXISTS gmail_messages (
    gmail_id TEXT PRIMARY KEY,
    job_id INTEGER REFERENCES jobs(id),
    sender TEXT, subject TEXT, received TEXT, snippet TEXT,
    suggested_status TEXT,
    company TEXT, title TEXT,  -- set when job_id is NULL: a company the app does not know yet
    state TEXT NOT NULL  -- ignored | pending | accepted | dismissed
);
CREATE TABLE IF NOT EXISTS master_cache (
    sha256 TEXT PRIMARY KEY,
    resume_json TEXT,
    parsed_at TEXT
);
CREATE TABLE IF NOT EXISTS resumes (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    path TEXT,
    master_sha256 TEXT,
    created_at TEXT,
    gaps_json TEXT,
    fallbacks_json TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS profile (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS resume_chat (
    id INTEGER PRIMARY KEY,
    job_id INTEGER REFERENCES jobs(id),
    role TEXT CHECK(role IN ('user','app')),
    text TEXT,
    at TEXT
);
CREATE TABLE IF NOT EXISTS interviews (
    uid TEXT PRIMARY KEY,
    gmail_id TEXT,
    job_id INTEGER REFERENCES jobs(id),
    company TEXT,
    start TEXT,  -- ISO-8601, UTC with offset
    end TEXT,
    summary TEXT,
    location TEXT,
    link TEXT,
    sender TEXT,
    subject TEXT,
    status TEXT  -- scheduled | cancelled
);
CREATE TABLE IF NOT EXISTS interview_seen (
    gmail_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,  -- import | scan | gmail | interviews | grade | digest
    started_at TEXT NOT NULL,
    finished_at TEXT,
    counts_json TEXT,
    error TEXT,
    path TEXT
);
CREATE TABLE IF NOT EXISTS job_materials (
    id INTEGER PRIMARY KEY,
    job_id INTEGER REFERENCES jobs(id),
    folder TEXT,
    kind TEXT,  -- resume_pdf | resume_md | resume_docx | cover_letter | apply_sheet | warm_intros | posting
    path TEXT UNIQUE,
    mtime TEXT
);
"""

_JOB_UPDATE_FIELDS = (
    "company", "title", "location", "salary_min", "salary_max",
    "description", "source", "posted_at",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Columns added after a table first shipped: (table, column, type)
_ADDED_COLUMNS = [
    ("gmail_messages", "company", "TEXT"),
    ("gmail_messages", "title", "TEXT"),
    ("resumes", "file_sha256", "TEXT"),
    ("resumes", "reqs_json", "TEXT"),
    ("resumes", "matches_json", "TEXT"),
    ("resumes", "draft_json", "TEXT"),
    ("resumes", "final_at", "TEXT"),
    ("jobs", "prof_score", "INTEGER"),
    ("jobs", "prof_reason", "TEXT"),
    ("jobs", "added_at", "TEXT"),
    ("job_events", "source", "TEXT"),
    ("people", "kind", "TEXT"),  # NULL = a found contact
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, kind in _ADDED_COLUMNS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    # Proficiently scores live on the job now, not as a fake fit. A surviving fit is newer than the
    # column (the importer writes one per run), so it wins. Each step is guarded so an already
    # migrated database runs no write at all.
    if conn.execute("SELECT 1 FROM fits WHERE model = 'proficiently-import' LIMIT 1").fetchone():
        conn.execute(
            """UPDATE jobs SET
                 prof_score = COALESCE((SELECT f.llm_score FROM fits f
                                WHERE f.job_id = jobs.id AND f.model = 'proficiently-import'), prof_score),
                 prof_reason = COALESCE((SELECT f.reasons FROM fits f
                                WHERE f.job_id = jobs.id AND f.model = 'proficiently-import'), prof_reason)
               WHERE id IN (SELECT job_id FROM fits WHERE model = 'proficiently-import')""")
        conn.execute("DELETE FROM fits WHERE model = 'proficiently-import'")
    if conn.execute("SELECT 1 FROM jobs WHERE source = 'import' LIMIT 1").fetchone():
        conn.execute("UPDATE jobs SET source = 'proficiently' WHERE source = 'import'")
    if conn.execute("SELECT 1 FROM jobs WHERE added_at IS NULL AND first_seen IS NOT NULL LIMIT 1").fetchone():
        conn.execute("UPDATE jobs SET added_at = first_seen WHERE added_at IS NULL AND first_seen IS NOT NULL")
    conn.commit()


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
        now = _now()
        cur = conn.execute(
            """INSERT INTO jobs (company, title, url, location, salary_min, salary_max,
                   description, source, posted_at, first_seen, status, flags, added_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (j.company, j.title, url, j.location, j.salary_min, j.salary_max,
             j.description, j.source, j.posted_at, j.first_seen or now,
             j.status, json.dumps(j.flags), now),
        )
        conn.commit()
        return cur.lastrowid, True

    updates = {f: getattr(j, f) for f in _JOB_UPDATE_FIELDS if getattr(j, f) is not None}
    if j.flags or j.source != "proficiently":  # a scan re-evaluates flags, so [] must clear stale ones
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
        where.append("COALESCE(f.llm_score, j.prof_score) >= ?")
        args.append(min_score)
    if company is not None:
        where.append("j.company = ?")
        args.append(company)
    sql = (
        "SELECT j.*, f.keyword_score, f.llm_score, f.match_pct, f.error AS fit_error, f.model AS fit_model, "
        "j.prof_score, j.prof_reason "
        "FROM jobs j LEFT JOIN fits f ON f.job_id = j.id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(f.llm_score, j.prof_score) DESC, j.id DESC"
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


def set_job_status(conn: sqlite3.Connection, job_id: int, status: str,
                   note: str | None = None, *, source: str | None = None) -> None:
    """Set a job's status and log the change. Re-saving the same status logs only a note."""
    note = (note or "").strip() or None
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return
    if row["status"] != status or note:
        conn.execute("INSERT INTO job_events (job_id, status, note, at, source) VALUES (?, ?, ?, ?, ?)",
                     (job_id, status, note, _now(), source))
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    conn.commit()


def list_events(conn: sqlite3.Connection, job_id: int) -> list[dict]:
    rows = conn.execute("SELECT status, note, at FROM job_events WHERE job_id = ? ORDER BY id",
                        (job_id,)).fetchall()
    return [dict(r) for r in rows]


def pipeline_board(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Jobs in an application stage, grouped by stage, oldest in stage first."""
    stages = PIPELINE + CLOSED
    marks = ",".join("?" * len(stages))
    rows = conn.execute(
        f"""SELECT j.id, j.company, j.title, j.url, j.status, f.llm_score,
               COALESCE((SELECT MIN(e.at) FROM job_events e
                         WHERE e.job_id = j.id AND e.status = j.status
                           AND NOT EXISTS (SELECT 1 FROM job_events l WHERE l.job_id = j.id
                                           AND l.id > e.id AND l.status != j.status)),
                        j.first_seen) AS stage_since,
               (SELECT MIN(e.at) FROM job_events e
                 WHERE e.job_id = j.id AND e.status = 'applied') AS applied_at,
               (SELECT e.note FROM job_events e WHERE e.job_id = j.id AND e.note IS NOT NULL
                 ORDER BY e.id DESC LIMIT 1) AS last_note
           FROM jobs j LEFT JOIN fits f ON f.job_id = j.id
           WHERE j.status IN ({marks}) ORDER BY stage_since, j.id""",
        stages,
    ).fetchall()
    board: dict[str, list[dict]] = {s: [] for s in stages}
    for r in rows:
        board[r["status"]].append(dict(r))
    return board


# --- gmail -----------------------------------------------------------------

def gmail_seen_ids(conn: sqlite3.Connection, *, settled_only: bool = False) -> set[str]:
    """Emails not to fetch again. With settled_only, only ones you accepted or dismissed."""
    sql = "SELECT gmail_id FROM gmail_messages"
    if settled_only:
        sql += " WHERE state IN ('accepted', 'dismissed')"
    return {r[0] for r in conn.execute(sql)}


def jobs_for_matching(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT id, company, title, status FROM jobs")]


_INTERVIEW_COLS = ("uid", "gmail_id", "job_id", "company", "start", "end", "summary", "location",
                   "link", "sender", "subject", "status")


def upsert_interview(conn: sqlite3.Connection, row: dict) -> None:
    vals = [row.get(c) for c in _INTERVIEW_COLS]
    sets = ", ".join(
        f"{c} = COALESCE(excluded.{c}, interviews.{c})" if c in ("job_id", "company")
        else f"{c} = excluded.{c}" for c in _INTERVIEW_COLS[1:])
    conn.execute(
        f"INSERT INTO interviews ({', '.join(_INTERVIEW_COLS)}) "
        f"VALUES ({', '.join('?' * len(_INTERVIEW_COLS))}) ON CONFLICT(uid) DO UPDATE SET {sets}",
        vals)
    conn.commit()


def list_interviews(conn: sqlite3.Connection, *, upcoming_only: bool = True, now: str) -> list[dict]:
    sql = ("SELECT i.*, j.title AS job_title, j.status AS job_status FROM interviews i "
           "LEFT JOIN jobs j ON j.id = i.job_id WHERE COALESCE(i.status, '') != 'cancelled'")
    args: list = []
    if upcoming_only:
        sql += " AND i.start >= ?"
        args.append(now)
    return [dict(r) for r in conn.execute(sql + " ORDER BY i.start", args)]


def interview_seen_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT gmail_id FROM interview_seen")}


def mark_interview_seen(conn: sqlite3.Connection, gmail_id: str) -> None:
    conn.execute("INSERT OR IGNORE INTO interview_seen (gmail_id) VALUES (?)", (gmail_id,))
    conn.commit()


def save_gmail_message(conn: sqlite3.Connection, gmail_id: str, *, job_id: int | None = None,
                       sender: str | None = None, subject: str | None = None,
                       received: str | None = None, snippet: str | None = None,
                       suggested_status: str | None = None, company: str | None = None,
                       title: str | None = None) -> None:
    """Insert a Gmail message row, or re-decide one that is still ignored or pending."""
    state = "pending" if suggested_status else "ignored"
    conn.execute(
        """INSERT INTO gmail_messages (gmail_id, job_id, sender, subject, received, snippet,
               suggested_status, state, company, title) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(gmail_id) DO UPDATE SET job_id = excluded.job_id, sender = excluded.sender,
               subject = excluded.subject, received = excluded.received,
               snippet = excluded.snippet, suggested_status = excluded.suggested_status,
               state = excluded.state, company = excluded.company, title = excluded.title
           WHERE gmail_messages.state IN ('ignored', 'pending')""",
        (gmail_id, job_id, sender, subject, received, snippet, suggested_status, state,
         company, title))
    conn.commit()


def list_gmail_suggestions(conn: sqlite3.Connection) -> list[dict]:
    """Pending suggestions that would still move a job forward, newest email first.
    Emails about a company the app does not know yet collapse into one 'add' suggestion
    per company, at the furthest stage any of them reached."""
    from .gmail.classify import should_suggest, stage_rank

    rows = conn.execute(
        """SELECT g.rowid AS id, g.gmail_id, g.job_id, g.sender, g.subject, g.received,
                  g.snippet, g.suggested_status, COALESCE(j.company, g.company) AS company,
                  COALESCE(j.title, g.title) AS title, j.status AS current_status
           FROM gmail_messages g LEFT JOIN jobs j ON j.id = g.job_id
           WHERE g.state = 'pending' ORDER BY g.received DESC, g.rowid DESC""").fetchall()
    # One suggestion per job (or per new company): the furthest stage, newest email for ties.
    # Rows arrive newest first, so the first row seen at a stage is the newest one.
    best: dict = {}
    for r in map(dict, rows):
        if r["job_id"] is not None and not should_suggest(r["current_status"], r["suggested_status"]):
            continue
        key = r["job_id"] if r["job_id"] is not None else "new:" + r["company"].lower()
        rank = 99 if r["suggested_status"] in ("rejected", "withdrawn") else stage_rank(
            r["suggested_status"])
        if key not in best or rank > best[key][0]:
            best[key] = (rank, r)
    out = [r for _, r in best.values()]
    return sorted(out, key=lambda r: (r["received"] or "", r["id"]), reverse=True)


def _slug(text: str) -> str:
    return "-".join("".join(ch if ch.isalnum() else " " for ch in text.lower()).split()) or "x"


def resolve_gmail_suggestion(conn: sqlite3.Connection, suggestion_id: int, *, accept: bool) -> bool:
    row = conn.execute("SELECT * FROM gmail_messages WHERE rowid = ? AND state = 'pending'",
                       (suggestion_id,)).fetchone()
    if row is None:
        return False
    job_id = row["job_id"]
    if job_id is None:
        same_company = "job_id IS NULL AND state = 'pending' AND lower(company) = lower(?)"
        if not accept:
            conn.execute(f"UPDATE gmail_messages SET state = 'dismissed' WHERE {same_company}",
                         (row["company"],))
            conn.commit()
            return True
        title = row["title"] or "(added from Gmail)"
        job_id, _ = upsert_job(conn, Job(
            company=row["company"], title=title, source="gmail",
            url=f"gmail://{_slug(row['company'])}/{_slug(title)}"))
        conn.execute(f"UPDATE gmail_messages SET job_id = ? WHERE {same_company}",
                     (job_id, row["company"]))
    conn.execute("UPDATE gmail_messages SET state = ? WHERE rowid = ?",
                 ("accepted" if accept else "dismissed", suggestion_id))
    conn.commit()
    if accept:
        set_job_status(conn, job_id, row["suggested_status"],
                       note=f"From Gmail: {row['subject']} ({row['received']})", source="gmail")
    return True


# --- tailored resume: parsed master cache ----------------------------------

def get_master_cache(conn: sqlite3.Connection, sha: str) -> str | None:
    row = conn.execute("SELECT resume_json FROM master_cache WHERE sha256 = ?", (sha,)).fetchone()
    return row[0] if row else None


def save_master_cache(conn: sqlite3.Connection, sha: str, resume_json: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO master_cache (sha256, resume_json, parsed_at) VALUES (?, ?, ?)",
        (sha, resume_json, _now()))
    conn.commit()


# --- tailored resume: per-job output record --------------------------------

def save_resume(conn: sqlite3.Connection, rec: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO resumes
           (job_id, path, master_sha256, created_at, gaps_json, fallbacks_json, error, file_sha256,
            reqs_json, matches_json, draft_json, final_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (rec["job_id"], rec.get("path"), rec.get("master_sha256"), _now(),
         json.dumps(rec.get("gaps") or []), json.dumps(rec.get("fallbacks") or []),
         rec.get("error"), rec.get("file_sha256"),
         rec.get("reqs_json"), rec.get("matches_json"), rec.get("draft_json"),
         rec.get("final_at")))  # a fresh save passes no final_at (so it is cleared); _fail passes the old row's
    conn.commit()


def get_resume(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM resumes WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    rec = dict(row)
    rec["gaps"] = json.loads(rec.pop("gaps_json") or "[]")
    rec["fallbacks"] = json.loads(rec.pop("fallbacks_json") or "[]")
    return rec


def resume_paths(conn: sqlite3.Connection, exclude_job: int | None = None) -> set[str]:
    rows = conn.execute("SELECT path FROM resumes WHERE path IS NOT NULL AND job_id IS NOT ?",
                        (exclude_job,))
    return {r[0] for r in rows}


# --- resume review chat ------------------------------------------------------

def add_chat(conn: sqlite3.Connection, job_id: int, role: str, text: str) -> None:
    conn.execute("INSERT INTO resume_chat (job_id, role, text, at) VALUES (?, ?, ?, ?)",
                 (job_id, role, text, _now()))
    conn.commit()


def list_chat(conn: sqlite3.Connection, job_id: int) -> list[dict]:
    rows = conn.execute("SELECT id, job_id, role, text, at FROM resume_chat WHERE job_id = ? ORDER BY id",
                        (job_id,))
    return [dict(r) for r in rows]


def set_final(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute("UPDATE resumes SET final_at = ? WHERE job_id = ?", (_now(), job_id))
    conn.commit()


def final_job_ids(conn: sqlite3.Connection) -> set[int]:
    return {r[0] for r in conn.execute("SELECT job_id FROM resumes WHERE final_at IS NOT NULL")}


# --- runs ------------------------------------------------------------------

def start_run(conn: sqlite3.Connection, kind: str, path: str | None = None) -> int:
    cur = conn.execute("INSERT INTO runs (kind, started_at, path) VALUES (?, ?, ?)",
                       (kind, _now(), path))
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, counts: dict | None = None,
               error: str | None = None) -> None:
    conn.execute("UPDATE runs SET finished_at = ?, counts_json = ?, error = ? WHERE id = ?",
                 (_now(), json.dumps(counts) if counts is not None else None, error, run_id))
    conn.commit()


def _run_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["counts"] = json.loads(d["counts_json"]) if d.get("counts_json") else None
    return d


def last_runs(conn: sqlite3.Connection) -> dict[str, dict]:
    """The latest run of each kind."""
    rows = conn.execute(
        "SELECT * FROM runs WHERE id IN (SELECT MAX(id) FROM runs GROUP BY kind)").fetchall()
    return {r["kind"]: _run_row(r) for r in rows}


def runs_on(conn: sqlite3.Connection, local_date: date) -> list[dict]:
    """Runs that started on the given local calendar date, oldest first."""
    out = []
    for r in conn.execute("SELECT * FROM runs ORDER BY id").fetchall():
        started = datetime.fromisoformat(r["started_at"])
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if started.astimezone().date() == local_date:
            out.append(_run_row(r))
    return out


# --- job materials ---------------------------------------------------------

def save_material(conn: sqlite3.Connection, job_id: int, folder: str, kind: str,
                  path: str, mtime: str) -> None:
    conn.execute(
        """INSERT INTO job_materials (job_id, folder, kind, path, mtime) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET job_id = excluded.job_id, folder = excluded.folder,
             kind = excluded.kind, mtime = excluded.mtime""",
        (job_id, folder, kind, path, mtime))
    conn.commit()


def list_materials(conn: sqlite3.Connection, job_id: int) -> list[dict]:
    """A job's files, newest folder first."""
    rows = conn.execute(
        "SELECT * FROM job_materials WHERE job_id = ? ORDER BY folder DESC, kind, id",
        (job_id,)).fetchall()
    return [dict(r) for r in rows]


def material_paths(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT path FROM job_materials")}
