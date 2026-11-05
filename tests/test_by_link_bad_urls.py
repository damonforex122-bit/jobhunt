import httpx
import pytest

from jobhunt import cli
from jobhunt.config import Settings
from jobhunt.sources import by_link
from jobhunt.sources.by_link import LinkError, check_public_url


def test_malformed_url_is_a_link_error():
    with pytest.raises(LinkError) as e:
        check_public_url("http://[zz/job")
    assert not e.value.needs_text and "valid web link" in str(e.value)


def test_overlong_dns_label_is_a_link_error():
    # real _resolve: idna rejects the label before any lookup happens
    assert by_link._resolve("a" * 70 + ".example.com") == []
    with pytest.raises(LinkError) as e:
        check_public_url("https://" + "a" * 70 + ".example.com/job")
    assert e.value.needs_text


def test_invalid_redirect_location_is_a_link_error(monkeypatch):
    monkeypatch.setattr(by_link, "_resolve", lambda h: ["93.184.216.34"])
    http = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(302, headers={"location": "http://exa mple.com:badport/x"})))
    with pytest.raises(LinkError) as e:
        by_link._fetch_page(http, "https://careers.example.test/start")
    assert e.value.needs_text


def test_api_get_invalid_url_is_a_link_error():
    class Bad:
        cookies = httpx.Cookies()

        def get(self, *a, **k):
            raise httpx.InvalidURL("nope")

    with pytest.raises(LinkError) as e:
        by_link._api_get(Bad(), "https://x.test/")
    assert e.value.needs_text


def test_cli_add_bad_url_prints_and_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("jobhunt.config.load_settings",
                        lambda: Settings(db_path=tmp_path / "l.db"))
    assert cli.main(["add", "http://[zz/job"]) == 1
    assert "valid web link" in capsys.readouterr().out
