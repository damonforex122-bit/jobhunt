import imaplib
from datetime import datetime, timedelta, timezone

import pytest

from jobhunt import db
from jobhunt.config import Settings
from jobhunt.gmail import connect
from jobhunt.gmail.auth import GmailNotConnected
from jobhunt.gmail.client import GmailClient, GmailError
from jobhunt.gmail.imap_client import ImapGmailClient
from jobhunt.gmail.interviews import run_interview_sync
from jobhunt.gmail.sync import run_sync
from jobhunt.models import Job

PASSWORD = "sekrit-app-pw"
PST = timezone(timedelta(hours=-8))
NOW = datetime(2026, 11, 5, tzinfo=PST)


def raw_mail(subject="Next steps with Acme", sender="Rita <rita@acme.com>", plain="Hi, let's talk.",
             html=None, extra_headers="", ics=None):
    parts = []
    if plain is not None:
        parts.append(f"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{plain}\r\n")
    if html:
        parts.append(f"--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n{html}\r\n")
    if ics:
        parts.append(f"--B\r\nContent-Type: text/calendar; method=REQUEST\r\n\r\n{ics}\r\n")
    return (f"From: {sender}\r\nSubject: {subject}\r\nDate: Wed, 04 Nov 2026 23:00:00 -0800\r\n"
            f"{extra_headers}MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n"
            + "".join(parts) + "--B--\r\n").encode()


class FakeImap:
    """messages: {uid: (x_gm_msgid_decimal, raw_bytes)}"""

    def __init__(self, messages, login_error=False):
        self.messages, self.login_error = messages, login_error
        self.calls, self.selected, self.logged_out, self.logged_in = [], None, False, None

    def login(self, user, password):
        if self.login_error:
            raise imaplib.IMAP4.error(f"[AUTHENTICATIONFAILED] {password} invalid")
        self.logged_in = (user, password)
        return "OK", [b"ok"]

    def select(self, mailbox, readonly=False):
        self.selected = (mailbox, readonly)
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u in self.messages)]
        uids = args[0].split(",")
        if args[1] == "(X-GM-MSGID)":
            return "OK", [f"{i} (X-GM-MSGID {self.messages[int(u)][0]} UID {u})".encode()
                          for i, u in enumerate(uids, 1)]
        uid = int(uids[0])
        gm, raw = self.messages[uid]
        head = (f'1 (X-GM-MSGID {gm} UID {uid} INTERNALDATE "05-Nov-2026 07:00:00 +0000" '
                f"BODY[] {{{len(raw)}}}").encode()
        return "OK", [(head, raw), b")"]

    def logout(self):
        self.logged_out = True


def client_for(fake):
    return ImapGmailClient("me@gmail.com", PASSWORD, imap_factory=lambda *a, **k: fake)


def test_connects_read_only_to_all_mail_with_timeout():
    fake, seen = FakeImap({1: (10, raw_mail())}), {}

    def factory(*a, **k):
        seen.update(args=a, kwargs=k)
        return fake

    c = ImapGmailClient("me@gmail.com", PASSWORD, imap_factory=factory)
    c.list_ids("x")
    assert seen["args"] == ("imap.gmail.com", 993) and seen["kwargs"] == {"timeout": 30}
    assert fake.logged_in == ("me@gmail.com", PASSWORD)
    assert fake.selected == ('"[Gmail]/All Mail"', True)


def test_search_passes_query_quoted_and_ids_are_hex_newest_first():
    fake = FakeImap({3: (255, raw_mail()), 7: (4096, raw_mail()), 5: (int("1a2b3c", 16), raw_mail())})
    c = client_for(fake)
    ids = c.list_ids('newer_than:30d (a OR "b c") \\x')
    assert ("SEARCH", "X-GM-RAW", '"newer_than:30d (a OR \\"b c\\") \\\\x"') in fake.calls
    assert ids == ["1000", "1a2b3c", "ff"]  # uid 7, 5, 3; no 0x, no leading zeros
    assert c.list_ids("x", limit=2) == ["1000", "1a2b3c"]


def test_get_uses_peek_and_parses_fields():
    html_only = raw_mail(plain=None, html="<p>Hello <b>there</b></p>",
                         extra_headers="List-Unsubscribe: <mailto:u@x.com>\r\n")
    fake = FakeImap({1: (255, raw_mail(plain="Hi,\r\n  let's   talk.")), 2: (256, html_only)})
    c = client_for(fake)
    ids = c.list_ids("x")
    m = c.get(ids[1])  # uid 1
    assert m.id == "ff" and m.sender == "Rita <rita@acme.com>" and m.subject == "Next steps with Acme"
    assert m.date == "2026-11-05" and "let's   talk" in m.body and m.snippet == "Hi, let's talk."
    assert m.bulk is False and m.ics == []
    fetch = [c_ for c_ in fake.calls if c_[0] == "FETCH" and "BODY" in c_[2]]
    assert fetch and all("BODY.PEEK[]" in f[2] and "BODY[]" not in f[2].replace("BODY.PEEK[]", "")
                         for f in fetch)
    h = c.get(ids[0])
    assert "Hello" in h.body and "<" not in h.body and h.bulk is True


def test_get_extracts_ics_and_list_id_marks_bulk():
    ics = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:u1\r\nEND:VEVENT\r\nEND:VCALENDAR"
    fake = FakeImap({1: (1, raw_mail(ics=ics, extra_headers="List-Id: <jobs.x.com>\r\n"))})
    c = client_for(fake)
    m = c.get(c.list_ids("x")[0])
    assert len(m.ics) == 1 and "UID:u1" in m.ics[0] and m.bulk is True


def test_get_unlisted_id_searches_by_gm_msgid():
    fake = FakeImap({4: (255, raw_mail())})
    m = client_for(fake).get("ff")
    assert m.id == "ff" and ("SEARCH", "X-GM-MSGID", "255") in fake.calls


def test_bad_login_message_has_no_password_and_logs_out():
    fake = FakeImap({}, login_error=True)
    with pytest.raises(GmailError) as e:
        client_for(fake).list_ids("x")
    assert "rejected the app password" in str(e.value) and PASSWORD not in str(e.value)
    assert PASSWORD not in repr(e.value.__cause__) and fake.logged_out


def test_socket_errors_become_gmail_error():
    def boom(*a, **k):
        raise TimeoutError("timed out")

    with pytest.raises(GmailError, match="reach Gmail"):
        ImapGmailClient("me@gmail.com", PASSWORD, imap_factory=boom).list_ids("x")


def test_context_manager_and_close_logout():
    fake = FakeImap({1: (1, raw_mail())})
    with client_for(fake) as c:
        c.list_ids("x")
        assert c.profile_email() == "me@gmail.com" and c.attachment("a", "b") == b""
    assert fake.logged_out


def test_make_gmail_client_precedence(tmp_path, monkeypatch):
    token = tmp_path / "t.json"
    token.write_text("{}")
    both = Settings(gmail_address="me@gmail.com", gmail_app_password=PASSWORD, gmail_token=token)
    assert isinstance(connect.make_gmail_client(both), ImapGmailClient)
    assert connect.gmail_connected(both)
    monkeypatch.setattr(connect, "access_token", lambda s: "tok")
    oauth = Settings(gmail_token=token)
    assert isinstance(connect.make_gmail_client(oauth), GmailClient) and connect.gmail_connected(oauth)
    half = Settings(gmail_address="me@gmail.com", gmail_token=tmp_path / "none.json")
    assert not connect.gmail_connected(half)
    with pytest.raises(GmailNotConnected, match="Setup page"):
        connect.make_gmail_client(half)


def patch_imap(monkeypatch, fake):
    monkeypatch.setattr(connect, "ImapGmailClient",
                        lambda a, p: ImapGmailClient(a, p, imap_factory=lambda *x, **k: fake))


def imap_settings(tmp_path):
    return Settings(db_path=tmp_path / "t.db", gmail_address="me@gmail.com", gmail_app_password=PASSWORD,
                    gmail_token=tmp_path / "none.json")


def test_run_sync_end_to_end_over_imap(conn, tmp_path, monkeypatch):
    db.upsert_job(conn, Job(company="Acme", title="Engineer", url="https://x.test/a", source="ats"))
    fake = FakeImap({1: (255, raw_mail(subject="Next steps with Acme",
                                       plain="We would like to schedule an interview.")),
                     2: (256, raw_mail(subject="Your weekly digest", plain="hi",
                                       extra_headers="List-Unsubscribe: <x>\r\n"))})
    patch_imap(monkeypatch, fake)
    report = run_sync(conn, imap_settings(tmp_path))
    assert report.scanned == 2 and report.suggested == 1
    assert db.list_gmail_suggestions(conn)[0]["suggested_status"] == "screening"
    assert {"ff", "100"} <= db.gmail_seen_ids(conn) and fake.logged_out
    assert run_sync(conn, imap_settings(tmp_path)).scanned == 0


def test_run_interview_sync_end_to_end_over_imap(conn, tmp_path, monkeypatch):
    ics = ("BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nUID:uid-1\r\nDTSTART:20261109T191500Z\r\n"
           "SUMMARY:Interview with Acme\r\nLOCATION:Zoom\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    fake = FakeImap({1: (255, raw_mail(subject="Invitation: Interview with Acme", ics=ics))})
    patch_imap(monkeypatch, fake)
    assert run_interview_sync(conn, imap_settings(tmp_path), tz=PST, now=NOW) == 1
    [row] = db.list_interviews(conn, now="2026-11-05T00:00:00+00:00")
    assert row["gmail_id"] == "ff" and row["start"].startswith("2026-11-09T19:15")
    assert fake.logged_out


def test_test_connection_reports_count(tmp_path, monkeypatch):
    patch_imap(monkeypatch, FakeImap({1: (1, raw_mail()), 2: (2, raw_mail())}))
    assert connect.test_connection(imap_settings(tmp_path)) == (
        "Connected as me@gmail.com — found 2 job emails in the last 30 days")
