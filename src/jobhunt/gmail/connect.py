"""Pick the Gmail client: app password over IMAP first, else the older Google sign-in token."""
import httpx

from ..config import Settings
from .auth import GmailNotConnected, access_token
from .client import GmailClient
from .imap_client import ImapGmailClient


def _imap_configured(settings: Settings) -> bool:
    return bool(settings.gmail_address and settings.gmail_app_password)


def gmail_connected(settings: Settings) -> bool:
    return _imap_configured(settings) or settings.gmail_token.exists()


def make_gmail_client(settings: Settings) -> GmailClient | ImapGmailClient:
    if _imap_configured(settings):
        return ImapGmailClient(settings.gmail_address, settings.gmail_app_password)
    if settings.gmail_token.exists():
        return GmailClient(access_token(settings), httpx.Client())
    raise GmailNotConnected("Connect Gmail on the Setup page.")


def close_client(client) -> None:
    close = getattr(client, "close", None)
    if close:
        close()


def test_connection(settings: Settings) -> str:
    from .sync import build_query

    client = make_gmail_client(settings)
    try:
        me = client.profile_email()
        found = len(client.list_ids(build_query(30), limit=500))
    finally:
        close_client(client)
    return f"Connected as {me} — found {found} job emails in the last 30 days"
