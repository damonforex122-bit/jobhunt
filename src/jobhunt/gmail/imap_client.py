"""Read-only Gmail over IMAP with an app password. Same surface as GmailClient.

Messages are fetched with BODY.PEEK and the mailbox is selected read-only, so nothing is
marked read or changed. Ids are X-GM-MSGID in lowercase hex, which is the id the Gmail API
returns, so ids from earlier OAuth syncs stay valid."""
import imaplib
import re
from datetime import datetime, timezone
from email import message_from_bytes, policy

from ..sources.ats import _strip_html
from .client import GmailError, Message

HOST, PORT, TIMEOUT = "imap.gmail.com", 993, 30
MAILBOX = '"[Gmail]/All Mail"'
BAD_PASSWORD = ("Gmail rejected the app password — check it, and that 2-Step Verification is on. "
                "Some work/school accounts don't allow app passwords.")
_MSGID = re.compile(rb"X-GM-MSGID (\d+)")
_UID = re.compile(rb"UID (\d+)")
_INTERNAL = re.compile(rb'INTERNALDATE "([^"]+)"')
_BATCH = 500


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _hex(msgid: bytes | str) -> str:
    return format(int(msgid), "x")


def _text(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


class ImapGmailClient:
    def __init__(self, address: str, app_password: str, imap_factory=imaplib.IMAP4_SSL):
        self.address = address
        self._password = app_password
        self._factory = imap_factory
        self._imap = None
        self._uids: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        imap, self._imap = self._imap, None
        if imap is not None:
            try:
                imap.logout()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass

    def _connect(self):
        if self._imap is not None:
            return self._imap
        try:
            imap = self._factory(HOST, PORT, timeout=TIMEOUT)
        except OSError as exc:
            raise GmailError(f"Couldn't reach Gmail ({type(exc).__name__})") from None
        try:
            try:
                imap.login(self.address, self._password)
            except imaplib.IMAP4.error:
                raise GmailError(BAD_PASSWORD) from None
            typ, _ = imap.select(MAILBOX, readonly=True)
            if typ != "OK":
                raise GmailError("Couldn't open the All Mail folder — is IMAP turned on in Gmail settings?")
        except BaseException:
            try:
                imap.logout()
            except Exception:  # noqa: BLE001
                pass
            raise
        self._imap = imap
        return imap

    def _uid(self, *args):
        imap = self._connect()
        try:
            typ, data = imap.uid(*args)
        except imaplib.IMAP4.error:
            raise GmailError("Gmail IMAP command failed") from None
        except OSError as exc:
            self.close()
            raise GmailError(f"Lost the connection to Gmail ({type(exc).__name__})") from None
        if typ != "OK":
            raise GmailError("Gmail IMAP command failed")
        return data

    def list_ids(self, query: str, limit: int = 200) -> list[str]:
        data = self._uid("SEARCH", "X-GM-RAW", _quote(query))
        uids = sorted((int(u) for u in b" ".join(x for x in data if isinstance(x, bytes)).split()),
                      reverse=True)[:limit]
        ids: dict[int, str] = {}
        for i in range(0, len(uids), _BATCH):
            batch = ",".join(str(u) for u in uids[i:i + _BATCH])
            for item in self._uid("FETCH", batch, "(X-GM-MSGID)"):
                line = item[0] if isinstance(item, tuple) else item
                if not isinstance(line, bytes):
                    continue
                m, u = _MSGID.search(line), _UID.search(line)
                if m and u:
                    ids[int(u.group(1))] = _hex(m.group(1))
        out = []
        for uid in uids:
            if uid in ids:
                self._uids[ids[uid]] = str(uid)
                out.append(ids[uid])
        return out

    def _find_uid(self, mid: str) -> str:
        if mid not in self._uids:
            data = self._uid("SEARCH", "X-GM-MSGID", str(int(mid, 16)))
            found = b" ".join(x for x in data if isinstance(x, bytes)).split()
            if not found:
                raise GmailError("That email is no longer in Gmail")
            self._uids[mid] = found[0].decode()
        return self._uids[mid]

    def get(self, mid: str) -> Message:
        uid = self._find_uid(mid)
        data = self._uid("FETCH", uid, "(BODY.PEEK[] X-GM-MSGID INTERNALDATE)")
        raw, meta = b"", b""
        for item in data:
            if isinstance(item, tuple):
                meta, raw = meta + item[0], item[1]
            elif isinstance(item, bytes):
                meta += item
        if not raw:
            raise GmailError("Gmail returned an empty message")
        return _parse(mid, raw, meta)

    def attachment(self, mid: str, attachment_id: str) -> bytes:
        return b""

    def profile_email(self) -> str:
        return self.address


def _parse(mid: str, raw: bytes, meta: bytes) -> Message:
    msg = message_from_bytes(raw, policy=policy.default)
    plain, html, ics = [], [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        name = (part.get_filename() or "").lower()
        if ctype == "text/calendar" or name.endswith(".ics"):
            ics.append(_text(part))
        elif part.get_content_disposition() == "attachment":
            continue
        elif ctype == "text/plain":
            plain.append(_text(part))
        elif ctype == "text/html":
            html.append(_text(part))
    body = "\n".join(plain) if plain else _strip_html("\n".join(html))
    return Message(id=mid, sender=str(msg.get("From", "")), subject=str(msg.get("Subject", "")),
                   date=_date(msg, meta), snippet=" ".join(body.split())[:200], body=body,
                   bulk="List-Unsubscribe" in msg or "List-Id" in msg, ics=ics)


def _date(msg, meta: bytes) -> str:
    m = _INTERNAL.search(meta)
    when = None
    if m:
        try:
            when = datetime.strptime(m.group(1).decode().strip(), "%d-%b-%Y %H:%M:%S %z")
        except ValueError:
            pass
    if when is None:
        try:
            when = msg["Date"].datetime
        except Exception:  # noqa: BLE001 - missing or malformed Date header
            return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d")
