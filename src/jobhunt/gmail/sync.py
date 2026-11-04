"""Turn recent recruiting emails into suggested pipeline moves. Never changes a job by itself."""
import sys
from dataclasses import dataclass

import httpx

from .. import db
from ..config import Settings
from .auth import GmailNotConnected, access_token, run_auth
from .classify import classify, match_job, should_suggest
from .client import GmailClient

KEYWORDS = ('interview OR application OR applying OR "next steps" OR onsite OR offer OR '
            'unfortunately OR recruiter OR "phone screen" OR candidacy')


def build_query(days: int) -> str:
    return f"newer_than:{days}d ({KEYWORDS}) -category:promotions -category:social"


@dataclass
class SyncReport:
    scanned: int = 0
    suggested: int = 0


def run_sync(conn, settings: Settings, client: GmailClient | None = None, days: int = 30,
             limit: int = 200) -> SyncReport:
    if client is None:
        client = GmailClient(access_token(settings), httpx.Client())
    seen = db.gmail_seen_ids(conn)
    jobs = db.jobs_for_matching(conn)
    report = SyncReport()
    for mid in client.list_ids(build_query(days), limit=limit):
        if mid in seen:
            continue
        msg = client.get(mid)
        report.scanned += 1
        stage = classify(msg.subject, msg.body)
        job = match_job(jobs, msg.sender, msg.subject, msg.body) if stage else None
        if job and should_suggest(job["status"], stage):
            db.save_gmail_message(conn, mid, job_id=job["id"], sender=msg.sender,
                                  subject=msg.subject, received=msg.date,
                                  snippet=msg.snippet[:300], suggested_status=stage)
            report.suggested += 1
        else:
            db.save_gmail_message(conn, mid)  # id only, so it is not fetched again
    return report


def register(sub) -> None:
    a = sub.add_parser("gmail-auth", help="Connect Gmail (read-only) for interview tracking")

    def auth_handler(args) -> int:
        from ..config import load_settings
        try:
            path = run_auth(load_settings())
        except GmailNotConnected as e:
            print(e, file=sys.stderr)
            return 1
        print(f"Gmail connected. Token saved to {path}")
        return 0

    a.set_defaults(handler=auth_handler)

    s = sub.add_parser("gmail-sync", help="Suggest pipeline moves from recent recruiting emails")
    s.add_argument("--days", type=int, default=30, help="How far back to look (default 30)")

    def sync_handler(args) -> int:
        from ..config import load_settings
        settings = load_settings()
        conn = db.connect(settings.db_path)
        try:
            report = run_sync(conn, settings, days=args.days)
        except GmailNotConnected as e:
            print(e, file=sys.stderr)
            return 1
        except httpx.HTTPError as e:
            print(f"Gmail request failed: {type(e).__name__}", file=sys.stderr)
            return 1
        finally:
            conn.close()
        print(f"Checked {report.scanned} new emails, {report.suggested} suggested moves. "
              "Review them on the Pipeline page.")
        return 0

    s.set_defaults(handler=sync_handler)
