"""`jobhunt migrate-profile`: carry the original install's values into .env and the profile.

Safe to run again: each step is skipped when its value is already there. It never writes
inside the legacy folders.
"""
import os
from pathlib import Path

from . import db, profile_store
from .config import _read_dotenv, load_settings
from .envfile import update_env
from .profile import load_preferences

OLD_AI_NOTES = (
    '- Say "building", not "built", when describing my benchmark; it is still in progress.\n'
    "- Never state the funding amount."
)


def _legacy_paths() -> dict[str, Path]:
    home = Path.home()  # at call time, so tests can redirect HOME
    return {
        "MASTER_RESUME_PATH": home / "Documents/Resume/Georgia Liu - Master Resume.pdf",
        "RESUME_OUTPUT_DIR": home / "Documents/Resume",
        "PROFICIENTLY_DIR": home / "Documents/Resume/.proficiently",
    }


def migrate(settings) -> list[str]:
    actions: list[str] = []

    dotenv = _read_dotenv(settings.env_path)
    changes: dict[str, str | None] = {}
    for key, path in _legacy_paths().items():
        if key not in dotenv and not os.environ.get(key) and path.exists():
            changes[key] = str(path)
    if changes:
        update_env(settings.env_path, changes)
        from .config import reload_settings
        reload_settings(settings)
        actions += [f"Set {k} in {settings.env_path}" for k in changes]

    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(settings.db_path)
    try:
        pdir = settings.proficiently_dir
        if profile_store.get_preferences(conn) is None and pdir is not None and Path(pdir).is_dir():
            profile_store.save_preferences(conn, load_preferences(pdir))
            actions.append("Saved job preferences")
        if not profile_store.get_ai_notes(conn):
            profile_store.save_ai_notes(conn, OLD_AI_NOTES)
            actions.append("Saved notes for the AI")
        if not profile_store.get(conn, "setup_done"):
            profile_store.put(conn, "setup_done", True)
            actions.append("Marked setup as done")
    finally:
        conn.close()
    return actions


def register(sub) -> None:
    p = sub.add_parser("migrate-profile",
                       help="Carry the original install's paths, preferences and notes into the new settings")

    def handler(args) -> int:
        for line in migrate(load_settings()) or ["nothing to change"]:
            print(line)
        return 0

    p.set_defaults(handler=handler)
