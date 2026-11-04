"""Scan watchlist / known ATS companies for jobs matching preferences."""
import re
import sqlite3
from dataclasses import dataclass, field

import httpx
import yaml

from . import db, filters
from .config import Settings
from .profile import load_preferences
from .sources import ats

_COMP_RE = re.compile(r"^Compensation:\s*(.+)$", re.M)


@dataclass
class ScanReport:
    companies_scanned: int = 0
    jobs_seen: int = 0
    jobs_kept: int = 0
    jobs_new: int = 0
    jobs_skipped: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    new_jobs: list[dict] = field(default_factory=list)


def _fill_salary(job):
    if job.salary_min is not None or job.salary_max is not None or not job.description:
        return job
    m = _COMP_RE.search(job.description)
    if not m:
        return job
    lo, hi = filters.parse_salary(m.group(1))
    return job.model_copy(update={"salary_min": lo, "salary_max": hi})


def run_scan(
    conn: sqlite3.Connection, settings: Settings, client: httpx.Client | None = None
) -> ScanReport:
    prefs = load_preferences(settings.proficiently_dir)
    for co in ats.load_watchlist(settings.watchlist_path):
        db.upsert_company(conn, co)
    companies = [c for c in db.list_companies(conn) if c.ats]

    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=20.0, headers={"User-Agent": "jobhunt/0.1"})
    report = ScanReport()
    try:
        for co in companies:
            report.companies_scanned += 1
            stats: dict = {}
            try:
                jobs = ats.fetch_jobs(co, client, stats)
            except ats.FetchError as e:
                report.errors[co.name] = e.reason
                db.set_company_scan(conn, co.name, error=e.reason)
                continue
            except Exception as e:  # never let one company abort the scan
                reason = f"unexpected error: {type(e).__name__}: {e}"[:200]
                report.errors[co.name] = reason
                db.set_company_scan(conn, co.name, error=reason)
                continue
            report.jobs_skipped += stats.get("skipped", 0)
            for job in jobs:
                report.jobs_seen += 1
                job = _fill_salary(job)
                if job.posted_at:
                    job = job.model_copy(update={"posted_at": job.posted_at[:10]})
                keep, flags = filters.evaluate(job, prefs)
                if not keep:
                    continue
                report.jobs_kept += 1
                _, is_new = db.upsert_job(conn, job.model_copy(update={"flags": flags}))
                if is_new:
                    report.jobs_new += 1
                    report.new_jobs.append(
                        {"company": job.company, "title": job.title,
                         "location": job.location, "flags": flags}
                    )
            db.set_company_scan(conn, co.name, error=None)
    finally:
        if owns_client:
            client.close()
    return report


def register(sub) -> None:
    from .config import load_settings, short_error

    p = sub.add_parser("scan", help="Scan job boards for matching openings")

    def handler(args) -> int:
        settings = load_settings()
        conn = db.connect(settings.db_path)
        try:
            report = run_scan(conn, settings)
        except (ValueError, yaml.YAMLError, OSError) as e:  # bad watchlist/preferences
            print(f"error: {short_error(e)}")
            return 1
        finally:
            conn.close()
        print(
            f"Scanned {report.companies_scanned} companies: {report.jobs_seen} jobs seen, "
            f"{report.jobs_kept} kept, {report.jobs_new} new"
            + (f", {report.jobs_skipped} malformed skipped" if report.jobs_skipped else "")
        )
        if report.errors:
            print("Errors:")
            for name, reason in report.errors.items():
                print(f"  {name}: {reason}")
        if report.new_jobs:
            print(f"\n{'Company':<20} {'Title':<50} {'Location':<30} Flags")
            for j in report.new_jobs:
                print(
                    f"{j['company'][:20]:<20} {j['title'][:50]:<50} "
                    f"{(j['location'] or '-')[:30]:<30} {', '.join(j['flags']) or '-'}"
                )
        return 0

    p.set_defaults(handler=handler)
