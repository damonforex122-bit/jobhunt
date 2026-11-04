import os
from pathlib import Path

from pydantic import BaseModel, ValidationError, field_validator


class Settings(BaseModel):
    proficiently_dir: Path = Path("~/Documents/Resume/.proficiently").expanduser()
    db_path: Path = Path("data/jobhunt.db")
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.5-flash-lite"
    apollo_api_key: str | None = None
    exa_api_key: str | None = None
    watchlist_path: Path = Path("config/watchlist.yaml")

    @field_validator("proficiently_dir", "db_path", "watchlist_path")
    @classmethod
    def _expand_home(cls, v: Path) -> Path:
        return Path(v).expanduser()


def short_error(exc: Exception) -> str:
    """One-line, bounded description of a predictable user-facing failure."""
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        loc = ".".join(str(x) for x in first["loc"])
        msg = f"invalid config ({loc}): {first['msg']}"
    else:
        msg = str(exc) or type(exc).__name__
    return " ".join(msg.split())[:200]


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip("'\"")
    return values


def load_settings(env_file: Path = Path(".env")) -> Settings:
    env = {**_read_dotenv(env_file), **os.environ}
    kwargs = {
        field: env[field.upper()]
        for field in Settings.model_fields
        if env.get(field.upper())  # empty values (as in .env.example) count as unset
    }
    return Settings(**kwargs)
