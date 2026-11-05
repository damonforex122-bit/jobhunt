import os
from pathlib import Path

from pydantic import BaseModel, ValidationError, field_validator


class Settings(BaseModel):
    proficiently_dir: Path = Path("~/Documents/Resume/.proficiently").expanduser()
    db_path: Path = Path("data/jobhunt.db")
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.5-flash-lite"
    # Backup providers for resume tailoring, tried in this order when Gemini fails.
    anthropic_api_key: str | None = None
    claude_model: str = "claude-opus-5-5"
    openrouter_api_key: str | None = None
    openrouter_model: str = "openai/gpt-5-mini"
    apollo_api_key: str | None = None
    exa_api_key: str | None = None
    watchlist_path: Path = Path("config/watchlist.yaml")
    # Gmail (read-only). Both files live in data/, which git ignores.
    gmail_client_secret: Path = Path("data/gmail_client_secret.json")
    gmail_token: Path = Path("data/gmail_token.json")
    master_resume_path: Path = Path("~/Documents/Resume/Georgia Liu - Master Resume.pdf").expanduser()
    resume_output_dir: Path = Path("~/Documents/Resume").expanduser()
    env_path: Path = Path(".env")

    @field_validator("proficiently_dir", "db_path", "watchlist_path", "gmail_client_secret",
                     "gmail_token", "master_resume_path", "resume_output_dir", "env_path")
    @classmethod
    def _expand_home(cls, v: Path) -> Path:
        return Path(v).expanduser()


KEY_FIELDS = ("gemini_api_key", "anthropic_api_key", "openrouter_api_key", "apollo_api_key",
              "exa_api_key")


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
    return Settings(**kwargs, env_path=env_file)


def reload_settings(settings: Settings) -> None:
    """Re-read settings.env_path and the environment, updating `settings` in place so that
    closures holding this instance see the new values."""
    fresh = load_settings(settings.env_path)
    for field in Settings.model_fields:
        value = getattr(fresh, field)
        if isinstance(value, Path):
            value = value.expanduser()
        setattr(settings, field, value)
    settings.env_path = Path(settings.env_path)
