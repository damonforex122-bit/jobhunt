"""Turn recent recruiting emails into suggested pipeline moves. Never changes a job by itself."""
import sys
from dataclasses import dataclass

import httpx

from .. import db
from ..config import Settings, short_error
from .auth import GmailNotConnected, access_token, run_auth
from email.utils import parseaddr

from .classify import classify, extract_company, is_ats_sender, match_job, should_suggest
from .client import GmailClient, GmailError

KEYWORDS = ('interview OR application OR applying OR "next steps" OR "next step" OR onsite OR '
            '"offer letter" OR "extend an offer" OR unfortunately OR recruiter OR "phone screen" OR '
            'candidacy OR "your interest"')


def build_query(days: int) -> str:
    return f"newer_than:{days}d ({KEYWORDS}) -category:promotions -category:social"


@dataclass
class SyncReport:
    scanned: int = 0
    suggested: int = 0


def run_sync(conn, settings: Settings, client: GmailClient | None = None, days: int = 30,
             limit: int = 200, recheck: bool = False) -> SyncReport:
    """`recheck` re-reads every email not yet accepted or dismissed (after the rules change)."""
    if client is None:
        client = GmailClient(access_token(settings), httpx.Client())
    me = client.profile_email().lower()
    seen = db.gmail_seen_ids(conn, settled_only=recheck)
    jobs = db.jobs_for_matching(conn)
    report = SyncReport()
    for mid in client.list_ids(build_query(days), limit=limit):
        if mid in seen:
            continue
        msg = client.get(mid)
        report.scanned += 1
        suggestion = _suggest(msg, jobs, me)
        if suggestion:
            db.save_gmail_message(conn, mid, sender=msg.sender, subject=msg.subject,
                                  received=msg.date, snippet=msg.snippet[:300], **suggestion)
            report.suggested += 1
        else:
            db.save_gmail_message(conn, mid)  # id only, so it is not fetched again
    return report


def _suggest(msg, jobs: list[dict], me: str) -> dict | None:
    if me and parseaddr(msg.sender)[1].lower() == me:
        return None  # your own replies
    if msg.bulk and not is_ats_sender(msg.sender):
        return None  # newsletters and marketing
    stage = classify(msg.subject, msg.body)
    if not stage:
        return None
    job = match_job(jobs, msg.sender, msg.subject, msg.body)
    if job:
        if should_suggest(job["status"], stage):
            return {"job_id": job["id"], "suggested_status": stage}
        return None
    company = extract_company(msg.sender, msg.subject)
    return {"company": company, "suggested_status": stage} if company else None


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
    s.add_argument("--recheck", action="store_true",
                   help="Re-read emails not yet accepted/dismissed (use after the rules change)")

    def sync_handler(args) -> int:
        from ..config import load_settings
        settings = load_settings()
        conn = db.connect(settings.db_path)
        try:
            report = run_sync(conn, settings, days=args.days, recheck=args.recheck)
        except GmailNotConnected as e:
            print(e, file=sys.stderr)
            return 1
        except GmailError as e:
            print(f"{e}\nEmails checked before the error are saved; run gmail-sync again to continue.",
                  file=sys.stderr)
            return 1
        except httpx.HTTPError as e:
            print(f"Gmail request failed: {type(e).__name__}", file=sys.stderr)
            return 1
            from .interviews import run_interview_sync
            try:
                note = f"{run_interview_sync(conn, settings, days=args.days)} interviews updated. "
            except Exception as e:  # noqa: BLE001 - suggestions already succeeded
                note = f"Couldn't read interviews: {short_error(e)}. "
        finally:
            conn.close()
        print(f"Checked {report.scanned} new emails, {report.suggested} suggested moves. "
              f"{note}Review them on the Pipeline and Interviews pages.")
        return 0

    s.set_defaults(handler=sync_handler)
