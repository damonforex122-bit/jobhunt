"""`jobhunt daily`: the morning run. Import, scan, Gmail, interviews, grading; each step is logged."""
import dataclasses
import sqlite3
import subprocess
from collections.abc import Callable

from . import db
from .config import Settings

Step = tuple[str, Callable[[sqlite3.Connection, Settings], dict]]

RECONNECT = "reconnect Gmail: run jobhunt gmail-auth"
# run_import writes its own `runs` row, so daily must not write a second one for it.
OWN_RUN_ROW = {"import"}


class StepSkipped(Exception):
    pass


def _counts(report) -> dict:
    if dataclasses.is_dataclass(report) and not isinstance(report, type):
        return dataclasses.asdict(report)
    if isinstance(report, dict):
        return report
    return {"result": report}


def _step_import(conn, settings) -> dict:
    from .importer import run_import

    return _counts(run_import(conn, settings.proficiently_dir))


def _step_scan(conn, settings) -> dict:
    from .scan import run_scan

    return _counts(run_scan(conn, settings))


def _step_gmail(conn, settings) -> dict:
    from .gmail.sync import run_sync

    return _counts(run_sync(conn, settings))


def _step_interviews(conn, settings) -> dict:
    from .gmail.interviews import run_interview_sync

    return {"synced": run_interview_sync(conn, settings)}


def _step_grade(conn, settings) -> dict:
    from . import fit

    if not settings.gemini_api_key:
        raise StepSkipped("no GEMINI_API_KEY")
    graded = failed = 0
    for job in db.list_jobs(conn, status="new"):
        old = db.get_fit(conn, job["id"])
        if old is not None and old.llm_score is not None and not old.error:
            continue  # a real grade; failed ones are retried
        try:
            result = fit.grade_job(conn, job["id"], settings)
        except Exception:  # one bad job must not stop the rest
            failed += 1
            continue
        if result.error:
            failed += 1
        else:
            graded += 1
    return {"graded": graded, "failed": failed}


def default_steps() -> list[Step]:
    return [("import", _step_import), ("scan", _step_scan), ("gmail", _step_gmail),
            ("interviews", _step_interviews), ("grade", _step_grade)]


def _needs_reconnect(exc: Exception) -> bool:
    from .gmail.auth import GmailNotConnected
    from .tailor.providers import _status

    return (isinstance(exc, GmailNotConnected) or "RefreshError" in type(exc).__name__
            or _status(exc) == 401)


def _error_text(exc: Exception, settings: Settings) -> str:
    from .config import KEY_FIELDS

    if _needs_reconnect(exc):
        return RECONNECT
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    for field in KEY_FIELDS:
        key = getattr(settings, field, None)
        if key:
            text = text.replace(key, "***")
    return text[:300]


def run_daily(conn: sqlite3.Connection, settings: Settings,
              steps: list[Step] | None = None) -> list[dict]:
    results = []
    for name, fn in steps if steps is not None else default_steps():
        run_id = None if name in OWN_RUN_ROW else db.start_run(conn, name)
        counts, error, status = None, None, "ok"
        try:
            counts = fn(conn, settings)
        except StepSkipped as exc:
            status, error = "skipped", f"skipped: {exc}"[:300]
        except Exception as exc:
            status = "failed"
            try:
                error = _error_text(exc, settings)
            except Exception:
                error = type(exc).__name__
        except BaseException:  # Ctrl-C / SystemExit: close the row, then let it propagate
            if run_id is not None:
                try:
                    db.finish_run(conn, run_id, error="interrupted")
                except Exception:
                    pass
            raise
        result = {"step": name, "status": status, "counts": counts, "error": error}
        if run_id is not None:
            try:
                db.finish_run(conn, run_id, counts=counts, error=error)
            except Exception as exc:  # a lost log line must not stop the morning run
                result["log_error"] = f"{type(exc).__name__}: {exc}"[:300]
        results.append(result)
    return results


def _fmt_counts(counts: dict | None) -> str:
    if not counts:
        return "done"
    return ", ".join(f"{k} {v}" for k, v in counts.items())


def _print_results(results: list[dict]) -> None:
    for r in results:
        if r["status"] == "ok":
            print(f"✓ {r['step']}: {_fmt_counts(r['counts'])}")
        elif r["status"] == "skipped":
            print(f"– {r['step']}: {r['error']}")
        else:
            print(f"✗ {r['step']}: {r['error']}")


def _print_status(conn: sqlite3.Connection) -> None:
    runs = db.last_runs(conn)
    if not runs:
        print("No runs yet.")
    for kind, run in sorted(runs.items()):
        detail = run["error"] or (_fmt_counts(run["counts"]) if run["finished_at"] else "running")
        print(f"{kind}: {run['started_at']}  {detail}")


def _print_schedule() -> None:
    from . import launchd

    when = (launchd.installed() or {}).get("StartCalendarInterval")
    if isinstance(when, dict) and "Hour" in when:
        print(f"Morning run: scheduled daily at {when['Hour']:02d}:{when.get('Minute', 0):02d}")
    else:
        print("Morning run: not scheduled — run `jobhunt daily --install`")


def register(sub) -> None:
    p = sub.add_parser("daily", help="Morning run: import, scan, Gmail, interviews, grading")
    p.add_argument("--status", action="store_true", help="Show the latest run of each kind")
    p.add_argument("--install", action="store_true", help="Schedule the morning run with launchd")
    p.add_argument("--uninstall", action="store_true", help="Remove the scheduled morning run")
    p.add_argument("--at", default="08:00", metavar="HH:MM", help="Time for --install (24-hour)")

    def handler(args) -> int:
        from . import launchd
        from .config import load_settings

        if args.install:
            try:
                path = launchd.install(args.at)
            except (ValueError, RuntimeError, OSError) as exc:
                print(f"Can't schedule: {exc}")
                return 1
            except subprocess.CalledProcessError as exc:
                print(f"launchctl failed (exit {exc.returncode})")
                return 1
            print(f"Scheduled daily at {args.at} ({path})")
            return 0
        if args.uninstall:
            print("Unscheduled." if launchd.uninstall() else "Nothing was scheduled.")
            return 0
        settings = load_settings()
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = db.connect(settings.db_path)
        try:
            if args.status:
                _print_status(conn)
                _print_schedule()
                return 0
            results = run_daily(conn, settings)
        finally:
            conn.close()
        _print_results(results)
        return 1 if any(r["status"] == "failed" for r in results) else 0

    p.set_defaults(handler=handler)
