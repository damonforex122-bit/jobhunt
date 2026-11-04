import pytest

from jobhunt.urls import ats_from_url, normalize_url

CANON = "https://jobs.ashbyhq.com/afterquery/a565ac3b"


@pytest.mark.parametrize(
    "url",
    [
        "https://JOBS.ashbyhq.com/AfterQuery/a565ac3b/application",
        "https://jobs.ashbyhq.com/AfterQuery/a565ac3b/",
        "https://jobs.ashbyhq.com/AfterQuery/a565ac3b?utm_source=x#top",
    ],
)
def test_normalize_url_collapses_variants(url):
    assert normalize_url(url) == CANON


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://jobs.ashbyhq.com/AfterQuery/a565ac3b", ("ashby", "AfterQuery")),
        ("https://jobs.ashbyhq.com/Applied%20Compute/123", ("ashby", "Applied Compute")),
        ("https://job-boards.greenhouse.io/anthropic/jobs/1", ("greenhouse", "anthropic")),
        ("https://boards.greenhouse.io/anthropic/jobs/1", ("greenhouse", "anthropic")),
        ("https://jobs.lever.co/acme/abc-123", ("lever", "acme")),
        ("https://www.builtinsf.com/job/x", None),
    ],
)
def test_ats_from_url(url, expected):
    assert ats_from_url(url) == expected


@pytest.mark.parametrize(
    "a,b",
    [
        ("https://jobs.ashbyhq.com/abridge/abc-1", "https://jobs.ashbyhq.com/Abridge/abc-1"),
        ("https://jobs.lever.co/Acme/ID-1", "https://jobs.lever.co/acme/id-1"),
        ("https://boards.greenhouse.io/anthropic/jobs/1",
         "https://job-boards.greenhouse.io/anthropic/jobs/1"),
    ],
)
def test_normalize_url_ats_case_and_host_variants(a, b):
    assert normalize_url(a) == normalize_url(b)


def test_normalize_url_preserves_gh_jid_only():
    a = normalize_url("https://databricks.com/company/careers/open-positions/job?gh_jid=111&utm=x")
    b = normalize_url("https://databricks.com/company/careers/open-positions/job?gh_jid=222")
    assert a != b and a.endswith("?gh_jid=111") and "utm" not in a
    assert normalize_url("https://x.com/j?utm=1") == "https://x.com/j"
