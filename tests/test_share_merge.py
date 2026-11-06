"""A friend's install has no Proficiently folder: the merged Proficiently code must not crash."""
from fastapi.testclient import TestClient

from jobhunt import daily, db
from jobhunt.config import Settings
from jobhunt.models import Job
from jobhunt.sources.proficiently_files import apply_sheet_needs
from jobhunt.web import app as webapp


def test_daily_import_is_skipped_without_a_proficiently_folder(tmp_path):
    s = Settings(db_path=tmp_path / "t.db")
    assert s.proficiently_dir is None
    conn = db.connect(s.db_path)
    try:
        [r] = daily.run_daily(conn, s, steps=[("import", daily._step_import)])
    finally:
        conn.close()
    assert r["status"] == "skipped" and "Proficiently" in r["error"]


def test_prof_file_links_404_without_a_proficiently_folder(tmp_path):
    s = Settings(db_path=tmp_path / "t.db")
    conn = db.connect(s.db_path)
    jid, _ = db.upsert_job(conn, Job(company="Acme", title="Eng", url="https://x.test/1", source="ats"))
    from jobhunt import profile_store
    profile_store.put(conn, "setup_done", True)
    conn.close()
    c = TestClient(webapp.create_app(s), base_url="http://127.0.0.1")
    assert c.get(f"/jobs/{jid}").status_code == 200


def test_apply_sheet_needs_section_for_any_name():
    text = "## Needs Alex\n- Portfolio link\n## Other\n- x\n"
    assert apply_sheet_needs(text) == ["- Portfolio link"]
    assert apply_sheet_needs("## NEEDS GEORGIA\n* Visa status\n") == ["* Visa status"]
