import plistlib
import subprocess
from pathlib import Path

import pytest

from jobhunt import cli, db, launchd
from jobhunt.config import Settings


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0)


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    entry = tmp_path / "bin" / "jobhunt"
    monkeypatch.setattr("jobhunt.launchd.shutil.which", lambda name: str(entry))
    monkeypatch.setattr("jobhunt.launchd.os.getuid", lambda: 501)
    s = Settings(db_path=tmp_path / "data" / "t.db", proficiently_dir=tmp_path / "prof")
    monkeypatch.setattr("jobhunt.config.load_settings", lambda: s)
    monkeypatch.setattr("jobhunt.launchd.AGENTS_DIR", tmp_path / "agents")
    return tmp_path, entry


def test_plist_contents():
    d = launchd.plist_dict(Path("/x/jobhunt"), Path("/w"), 7, 5, Path("/l/daily.log"))
    assert d == {
        "Label": "com.jobhunt.daily", "ProgramArguments": ["/x/jobhunt", "daily"],
        "WorkingDirectory": "/w", "StartCalendarInterval": {"Hour": 7, "Minute": 5},
        "StandardOutPath": "/l/daily.log", "StandardErrorPath": "/l/daily.log", "RunAtLoad": False}


def test_install_writes_plist_and_calls_launchctl(project):
    tmp_path, entry = project
    agents, run = tmp_path / "agents", FakeRunner()
    path = launchd.install("06:30", agents_dir=agents, runner=run)
    assert path == agents / "com.jobhunt.daily.plist"
    data = plistlib.loads(path.read_bytes())
    assert data["StartCalendarInterval"] == {"Hour": 6, "Minute": 30}
    assert data["ProgramArguments"] == [str(entry), "daily"]
    assert data["WorkingDirectory"] == str(tmp_path)
    assert data["StandardOutPath"].endswith("data/daily.log")
    assert [c[0][:3] for c in run.calls] == [
        ["launchctl", "bootout", "gui/501"], ["launchctl", "bootstrap", "gui/501"]]
    assert run.calls[0][0][3] == str(path) and run.calls[1][1].get("check") is True
    assert launchd.installed(agents)["Label"] == "com.jobhunt.daily"


def test_install_rejects_bad_time_and_missing_pyproject(project):
    tmp_path, _ = project
    run = FakeRunner()
    for bad in ["24:00", "8:00", "08:60", "0800", "ab:cd", ""]:
        with pytest.raises(ValueError):
            launchd.install(bad, agents_dir=tmp_path / "agents", runner=run)
    (tmp_path / "pyproject.toml").unlink()
    with pytest.raises(RuntimeError, match="pyproject"):
        launchd.install(agents_dir=tmp_path / "agents", runner=run)
    assert run.calls == [] and not (tmp_path / "agents").exists()


def test_install_rejects_missing_entry_point(project, monkeypatch):
    tmp_path, _ = project
    monkeypatch.setattr("jobhunt.launchd.shutil.which", lambda name: None)
    monkeypatch.setattr("jobhunt.launchd.sys.argv", ["/nowhere/pytest"])
    with pytest.raises(RuntimeError, match="jobhunt"):
        launchd.install(agents_dir=tmp_path / "agents", runner=FakeRunner())


def test_uninstall_removes(project):
    tmp_path, _ = project
    agents, run = tmp_path / "agents", FakeRunner()
    assert launchd.uninstall(agents_dir=agents, runner=run) is False
    launchd.install(agents_dir=agents, runner=FakeRunner())
    assert launchd.uninstall(agents_dir=agents, runner=run) is True
    assert launchd.installed(agents) is None
    assert run.calls[0][0][:3] == ["launchctl", "bootout", "gui/501"]


def test_status_reports_installed_schedule(project, capsys):
    tmp_path, _ = project
    launchd.install("07:05", agents_dir=tmp_path / "agents", runner=FakeRunner())
    db.connect(tmp_path / "data" / "t.db").close()
    assert cli.main(["daily", "--status"]) == 0
    assert "Morning run: scheduled daily at 07:05" in capsys.readouterr().out


def test_cli_install_bad_time_fails_cleanly(project, capsys):
    assert cli.main(["daily", "--install", "--at", "99:99"]) == 1
    assert "HH:MM" in capsys.readouterr().out
    assert not (project[0] / "agents").exists()


def test_install_removes_plist_when_bootstrap_fails(project):
    tmp_path, _ = project
    agents = tmp_path / "agents"

    def runner(cmd, **kw):
        if "bootstrap" in cmd:
            raise subprocess.CalledProcessError(5, cmd)
        return subprocess.CompletedProcess(cmd, 0)

    with pytest.raises(subprocess.CalledProcessError):
        launchd.install(agents_dir=agents, runner=runner)
    assert launchd.installed(agents) is None
