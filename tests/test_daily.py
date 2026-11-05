
import httpx
import pytest

from jobhunt import cli, daily, db
from jobhunt.config import Settings
from jobhunt.gmail.auth import GmailNotConnected


_REAL_STEPS = daily.default_steps


def _settings(tmp_path, **kw) -> Settings:
    return Settings(db_path=tmp_path / "t.db", proficiently_dir=tmp_path / "prof", **kw)


def _kinds(conn):
    return [r["kind"] for r in conn.execute("SELECT kind FROM runs ORDER BY id")]


def test_steps_run_in_order_and_each_logs_a_run(conn, tmp_path):
    order = []

    def mk(name):
        def fn(c, s):
            order.append(name)
            return {"n": len(order)}
        return (name, fn)

    results = daily.run_daily(conn, _settings(tmp_path), [mk("scan"), mk("gmail")])
    assert order == ["scan", "gmail"]
    assert [r["step"] for r in results] == ["scan", "gmail"]
    assert all(r["status"] == "ok" for r in results)
    assert _kinds(conn) == ["scan", "gmail"]
    runs = db.last_runs(conn)
    assert runs["scan"]["counts"] == {"n": 1} and runs["scan"]["error"] is None
    assert runs["scan"]["finished_at"]


def test_import_step_does_not_write_a_second_run_row(conn, tmp_path):
    def imp(c, s):
        rid = db.start_run(c, "import")  # run_import writes its own row
        db.finish_run(c, rid, {"jobs": 1})
        return {"jobs": 1}

    daily.run_daily(conn, _settings(tmp_path), [("import", imp)])
    assert _kinds(conn) == ["import"]


def test_failing_step_does_not_stop_others_and_exit_code_1(conn, tmp_path, monkeypatch, capsys):
    def boom(c, s):
        raise RuntimeError("kaput")

    ok = lambda c, s: {"x": 1}  # noqa: E731
    results = daily.run_daily(conn, _settings(tmp_path), [("scan", boom), ("gmail", ok)])
    assert [r["status"] for r in results] == ["failed", "ok"]
    assert db.last_runs(conn)["scan"]["error"] == "RuntimeError: kaput"

    s = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: s, raising=False)
    monkeypatch.setattr("jobhunt.config.load_settings", lambda: s)
    monkeypatch.setattr(daily, "default_steps", lambda: [("scan", boom), ("gmail", ok)])
    assert cli.main(["daily"]) == 1
    out = capsys.readouterr().out
    assert "✗ scan: RuntimeError: kaput" in out
    assert "✓ gmail: x 1" in out


def test_import_failure_is_reported_but_writes_no_extra_row(conn, tmp_path):
    def bad(c, s):
        raise OSError("gone")

    results = daily.run_daily(conn, _settings(tmp_path), [("import", bad), ("scan", lambda c, s: {})])
    assert [r["status"] for r in results] == ["failed", "ok"]
    assert _kinds(conn) == ["scan"]


class RefreshError(Exception):
    pass


def _http_401():
    req = httpx.Request("GET", "https://gmail.googleapis.com/x")
    return httpx.HTTPStatusError("401", request=req, response=httpx.Response(401, request=req))


@pytest.mark.parametrize("exc", [GmailNotConnected("no token"), RefreshError("expired"),
                                 _http_401()])
def test_gmail_not_connected_message(conn, tmp_path, exc):
    def fn(c, s):
        raise exc

    results = daily.run_daily(conn, _settings(tmp_path), [("gmail", fn)])
    assert results[0]["status"] == "failed"
    assert results[0]["error"] == "reconnect Gmail: run jobhunt gmail-auth"
    assert db.last_runs(conn)["gmail"]["error"] == "reconnect Gmail: run jobhunt gmail-auth"


def _grade_step():
    return dict(_REAL_STEPS())["grade"]


def test_skipped_grade_is_not_a_failure(conn, tmp_path, monkeypatch, capsys):
    s = _settings(tmp_path)  # no keys
    results = daily.run_daily(conn, s, [("grade", _grade_step())])
    assert results[0]["status"] == "skipped"
    assert db.last_runs(conn)["grade"]["error"] == "skipped: no GEMINI_API_KEY"

    monkeypatch.setattr("jobhunt.config.load_settings", lambda: s)
    monkeypatch.setattr(daily, "default_steps", lambda: [("grade", _grade_step())])
    assert cli.main(["daily"]) == 0
    assert "– grade: skipped: no GEMINI_API_KEY" in capsys.readouterr().out


def test_default_steps_order():
    assert [n for n, _ in _REAL_STEPS()] == ["import", "scan", "gmail", "interviews", "grade"]


def test_grade_step_grades_only_new_jobs_without_a_fit(conn, tmp_path, monkeypatch):
    from jobhunt.models import Fit, Job

    for i in range(3):
        db.upsert_job(conn, Job(company=f"C{i}", title="T", url=f"https://x.test/{i}",
                                source="manual"))
    first, second, third = sorted(j["id"] for j in db.list_jobs(conn))
    db.save_fit(conn, Fit(job_id=first, keyword_score=1, graded_at="2026-01-01"))
    called = []

    def fake(c, job_id, settings, client=None):
        called.append(job_id)
        if job_id == second:
            raise RuntimeError("llm down")
        return Fit(job_id=job_id, keyword_score=1, graded_at="2026-01-01", llm_score=7)

    monkeypatch.setattr("jobhunt.fit.grade_job", fake)
    out = _grade_step()(conn, _settings(tmp_path, gemini_api_key="k"))
    assert sorted(called) == sorted([second, third])
    assert out == {"graded": 1, "failed": 1}


def test_error_text_masks_keys(conn, tmp_path):
    s = _settings(tmp_path, gemini_api_key="AIzaSECRET123", anthropic_api_key="sk-ant-SECRET")

    def boom(c, st):
        raise RuntimeError("bad key AIzaSECRET123 and sk-ant-SECRET " + "x" * 600)

    results = daily.run_daily(conn, s, [("scan", boom)])
    err = db.last_runs(conn)["scan"]["error"]
    assert "SECRET" not in err and "***" in err
    assert len(err) <= 300 and results[0]["error"] == err


def test_cli_status_prints_last_runs(conn, tmp_path, monkeypatch, capsys):
    s = _settings(tmp_path)
    rid = db.start_run(conn, "scan")
    db.finish_run(conn, rid, {"jobs_kept": 12})
    rid = db.start_run(conn, "gmail")
    db.finish_run(conn, rid, error="reconnect Gmail: run jobhunt gmail-auth")
    conn.commit()
    monkeypatch.setattr("jobhunt.config.load_settings", lambda: s)
    assert cli.main(["daily", "--status"]) == 0
    out = capsys.readouterr().out
    assert "scan" in out and "jobs_kept" in out and "12" in out
    assert "gmail" in out and "reconnect Gmail" in out
    assert "launchd" not in out
