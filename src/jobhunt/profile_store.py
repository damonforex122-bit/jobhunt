"""User profile data kept in the DB `profile` table (JSON values)."""
import json
import sqlite3
from pathlib import Path

from pydantic import BaseModel

from .config import Settings
from .models import Preferences
from .profile import load_preferences


class Links(BaseModel):
    website: str = ""
    github: str = ""
    linkedin: str = ""


class MasterInfo(BaseModel):
    path: str
    sha256: str
    name: str
    uploaded_at: str


def get(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM profile WHERE key = ?", (key,)).fetchone()
    return default if row is None else json.loads(row[0])


def put(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO profile(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()


def get_links(conn: sqlite3.Connection) -> Links:
    return Links(**(get(conn, "links") or {}))


def save_links(conn: sqlite3.Connection, links: Links) -> None:
    put(conn, "links", links.model_dump())


def get_preferences(conn: sqlite3.Connection) -> Preferences | None:
    data = get(conn, "preferences")
    return None if data is None else Preferences(**data)


def save_preferences(conn: sqlite3.Connection, prefs: Preferences) -> None:
    put(conn, "preferences", prefs.model_dump())


def effective_preferences(conn: sqlite3.Connection, settings: Settings) -> Preferences:
    saved = get_preferences(conn)
    if saved is not None:
        return saved
    if settings.proficiently_dir is not None and Path(settings.proficiently_dir).is_dir():
        return load_preferences(settings.proficiently_dir)
    return Preferences(locations=[], salary_floor=0, search_terms=[], excluded_terms=[],
                       target_roles=[])


def get_ai_notes(conn: sqlite3.Connection) -> str:
    return get(conn, "ai_notes") or ""


def save_ai_notes(conn: sqlite3.Connection, text: str) -> None:
    put(conn, "ai_notes", text.strip())


def get_master(conn: sqlite3.Connection) -> MasterInfo | None:
    data = get(conn, "master")
    return None if data is None else MasterInfo(**data)


def save_master(conn: sqlite3.Connection, info: MasterInfo) -> None:
    put(conn, "master", info.model_dump())


def get_pending_master(conn: sqlite3.Connection) -> dict | None:
    """The uploaded-but-unconfirmed resume: {"info": MasterInfo dict, "summary": preview dict}."""
    return get(conn, "pending_master") or None


def save_pending_master(conn: sqlite3.Connection, info: MasterInfo, summary: dict, resume_json: str = "") -> None:
    put(conn, "pending_master", {"info": info.model_dump(), "summary": summary, "resume_json": resume_json})


def save_master_read(conn: sqlite3.Connection, sha: str, resume_json: str) -> None:
    """The read the user confirmed: `load_master_with_sha` serves it for this file's hash before the
    cache, so a cache wipe or parser change never triggers a fresh (unseen) AI read."""
    put(conn, "master_read", {"sha": sha, "resume_json": resume_json})


def clear_pending_master(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM profile WHERE key = 'pending_master'")
    conn.commit()


def effective_master_path(conn: sqlite3.Connection, settings: Settings) -> Path | None:
    info = get_master(conn)
    return Path(info.path) if info else settings.master_resume_path


def effective_output_dir(conn: sqlite3.Connection, settings: Settings) -> Path:
    saved = get(conn, "resume_output_dir")
    return Path(saved).expanduser() if saved else settings.resume_output_dir


def get_file_name(conn: sqlite3.Connection) -> str | None:
    return get(conn, "file_name") or None


def save_file_name(conn: sqlite3.Connection, name: str) -> None:
    put(conn, "file_name", name.strip())
