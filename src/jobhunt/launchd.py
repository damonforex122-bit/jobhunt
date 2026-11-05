"""Schedule `jobhunt daily` with a launchd user agent."""
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.jobhunt.daily"
AGENTS_DIR = Path("~/Library/LaunchAgents").expanduser()
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def plist_dict(entry: Path, workdir: Path, hour: int, minute: int, log_path: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [str(entry), "daily"],
        "WorkingDirectory": str(workdir),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "RunAtLoad": False,
    }


def plist_path(agents_dir: Path | None = None) -> Path:
    return (agents_dir if agents_dir is not None else AGENTS_DIR) / f"{LABEL}.plist"


def _find_entry() -> Path:
    found = shutil.which("jobhunt")
    if found:
        return Path(found)
    argv0 = Path(sys.argv[0]).resolve()
    if argv0.name == "jobhunt" and argv0.exists():
        return argv0
    raise RuntimeError("can't find the jobhunt command; install it (uv sync) and run from the project")


def install(at: str = "08:00", *, agents_dir: Path | None = None, runner=subprocess.run) -> Path:
    if not isinstance(at, str) or not _TIME.match(at):
        raise ValueError(f"--at must be HH:MM, 24-hour (got {at!r})")
    hour, minute = int(at[:2]), int(at[3:])
    entry = _find_entry()
    workdir = Path.cwd()
    if not (workdir / "pyproject.toml").exists():
        raise RuntimeError("run this from the jobhunt project folder (no pyproject.toml here)")
    from .config import load_settings

    log_path = load_settings().db_path.parent.resolve() / "daily.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    path = plist_path(agents_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(plist_dict(entry, workdir, hour, minute, log_path), f)
    domain = f"gui/{os.getuid()}"
    runner(["launchctl", "bootout", domain, str(path)], check=False, capture_output=True)
    runner(["launchctl", "bootstrap", domain, str(path)], check=True)
    return path


def uninstall(*, agents_dir: Path | None = None, runner=subprocess.run) -> bool:
    path = plist_path(agents_dir)
    if not path.exists():
        return False
    runner(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], check=False, capture_output=True)
    path.unlink()
    return True


def installed(agents_dir: Path | None = None) -> dict | None:
    try:
        with open(plist_path(agents_dir), "rb") as f:
            return plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
