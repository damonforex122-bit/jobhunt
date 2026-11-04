import sqlite3

import httpx
from pydantic import BaseModel

from jobhunt import db
from jobhunt.config import Settings
from jobhunt.models import Company, Person

from . import apollo, exa, posting


class FinderReport(BaseModel):
    people: list[Person]
    skipped: dict[str, str] = {}


def _short(msg: str) -> str:
    return msg[:200]


def find_people(
    conn: sqlite3.Connection, job_id: int, settings: Settings,
    client: httpx.Client | None = None,
) -> FinderReport:
    job = db.get_job(conn, job_id)
    if job is None:
        raise ValueError(f"no job with id {job_id}")
    company = db.get_company(conn, job["company"]) or Company(name=job["company"])
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=20.0, headers={"User-Agent": "jobhunt/0.1"})
    skipped: dict[str, str] = {}
    try:
        found = posting.extract_people(job["description"] or "", job["url"], company.name)
        titles = posting.manager_titles(job["title"])

        if not settings.apollo_api_key:
            skipped["apollo"] = "APOLLO_API_KEY not set"
        else:
            try:
                found += apollo.search_people(company, titles, settings.apollo_api_key, client)
            except Exception as e:
                skipped["apollo"] = _short(str(e) or type(e).__name__)

        if not settings.exa_api_key:
            skipped["exa"] = "EXA_API_KEY not set"
        else:
            try:
                found += exa.search_people(company.name, titles, settings.exa_api_key, client)
            except Exception as e:
                skipped["exa"] = _short(str(e) or type(e).__name__)
    finally:
        if owns_client:
            client.close()

    for p in found:
        db.upsert_person(conn, p.model_copy(update={"job_id": job_id}))
    return FinderReport(people=db.list_people(conn, company.name), skipped=skipped)


def register(sub) -> None:
    from jobhunt.config import load_settings

    p = sub.add_parser("people", help="Find likely hiring managers for a job")
    p.add_argument("job_id", type=int)

    def handler(args) -> int:
        settings = load_settings()
        conn = db.connect(settings.db_path)
        try:
            try:
                report = find_people(conn, args.job_id, settings)
            except ValueError as e:
                print(e)
                return 1
        finally:
            conn.close()
        if report.people:
            print(f"{'Name':<30} {'Title':<35} {'Source':<8} Link")
            for x in report.people:
                print(
                    f"{x.name[:30]:<30} {(x.title or '-')[:35]:<35} {x.source:<8} "
                    f"{x.linkedin_url or x.source_url}"
                )
        else:
            print("No people found.")
        for src, reason in report.skipped.items():
            print(f"Skipped {src}: {reason}")
        return 0

    p.set_defaults(handler=handler)
