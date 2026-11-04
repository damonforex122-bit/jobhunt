import pytest

from jobhunt.urls import ats_from_url, normalize_url

CANON = "https://jobs.ashbyhq.com/AfterQuery/a565ac3b"


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
