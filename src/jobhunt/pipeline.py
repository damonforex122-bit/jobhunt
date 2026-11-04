"""Application pipeline: move a job through applied -> screening -> interviewing -> offer."""
import sys

from . import db
from .config import load_settings
from .models import CLOSED, PIPELINE, STATUSES, TRIAGE

__all__ = ["CLOSED", "PIPELINE", "STATUSES", "TRIAGE", "register"]


def _status(args) -> int:
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        job = db.get_job(conn, args.job_id)
        if job is None:
            print(f"job {args.job_id} not found", file=sys.stderr)
            return 1
        db.set_job_status(conn, args.job_id, args.stage, note=args.note)
        print(f"{job['company']} - {job['title']}: {job['status']} -> {args.stage}")
        return 0
    finally:
        conn.close()


def _board(args) -> int:
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        board = db.pipeline_board(conn)
    finally:
        conn.close()
    stages = PIPELINE + (CLOSED if args.all else ())
    for stage in stages:
        jobs = board[stage]
        print(f"\n{stage} ({len(jobs)})")
        for j in jobs:
            note = f"  - {j['last_note']}" if j["last_note"] else ""
            since = (j["stage_since"] or "")[:10]
            print(f"  #{j['id']:<5} {j['company'][:20]:<20} {j['title'][:50]:<50} since {since}{note}")
    return 0


def register(sub) -> None:
    p = sub.add_parser("status", help="Move a job to a pipeline stage, with an optional note")
    p.add_argument("job_id", type=int)
    p.add_argument("stage", choices=STATUSES)
    p.add_argument("--note", help="e.g. 'Recruiter call Tue 2pm'")
    p.set_defaults(handler=_status)

    b = sub.add_parser("pipeline", help="Show jobs by application stage")
    b.add_argument("--all", action="store_true", help="Also show accepted/rejected/withdrawn")
    b.set_defaults(handler=_board)
