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
    return get_preferences(conn) or load_preferences(settings.proficiently_dir)


def get_master(conn: sqlite3.Connection) -> MasterInfo | None:
    data = get(conn, "master")
    return None if data is None else MasterInfo(**data)


def save_master(conn: sqlite3.Connection, info: MasterInfo) -> None:
    put(conn, "master", info.model_dump())


def effective_master_path(conn: sqlite3.Connection, settings: Settings) -> Path:
    info = get_master(conn)
    return Path(info.path) if info else settings.master_resume_path
