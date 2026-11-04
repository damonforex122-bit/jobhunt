"""One-time Google sign-in (read-only Gmail scope) and token refresh."""
import os
from pathlib import Path

from ..config import Settings

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailNotConnected(Exception):
    pass


def _save(creds, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json())
    os.chmod(path, 0o600)


def run_auth(settings: Settings) -> Path:
    """Open the browser for Google consent and store the token. Returns the token path."""
    if not settings.gmail_client_secret.exists():
        raise GmailNotConnected(
            f"OAuth client file not found at {settings.gmail_client_secret}. "
            "See README 'Gmail setup' to create one.")
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(settings.gmail_client_secret), SCOPES)
    creds = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True)
    _save(creds, settings.gmail_token)
    return settings.gmail_token


def access_token(settings: Settings) -> str:
    if not settings.gmail_token.exists():
        raise GmailNotConnected("Gmail not connected. Run: uv run jobhunt gmail-auth")
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(settings.gmail_token), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise GmailNotConnected("Gmail token expired. Run: uv run jobhunt gmail-auth")
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise GmailNotConnected(f"Gmail token refresh failed ({type(exc).__name__}). "
                                    "Run: uv run jobhunt gmail-auth") from None
        _save(creds, settings.gmail_token)
    return creds.token
