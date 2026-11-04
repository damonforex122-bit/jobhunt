import pytest

from jobhunt.gmail.classify import classify, match_job, should_suggest


@pytest.mark.parametrize("subject,body,stage", [
    ("Thank you for applying to Harvey", "We received your application.", "applied"),
    ("Your application to Exa", "Thanks for your interest in Exa! We have received your application", "applied"),
    ("Harvey - next steps", "We'd love to set up a phone screen with our recruiter.", "screening"),
    ("Intro call", "Could you share times for a 30 minute recruiter call?", "screening"),
    ("Onsite interview schedule", "Here is the agenda for your virtual onsite.", "interviewing"),
    ("Final round", "We'd like to invite you to the final round interviews.", "interviewing"),
    ("Technical interview", "Your technical interview with the team is confirmed.", "interviewing"),
    ("Offer", "We are thrilled to extend an offer to join Variance.", "offer"),
    ("Your application", "Unfortunately, we have decided not to move forward with your candidacy.", "rejected"),
    ("Update", "After careful consideration we will not be moving forward at this time.", "rejected"),
    ("Newsletter", "Our product launch this week.", None),
])
def test_classify(subject, body, stage):
    assert classify(subject, body) == stage


def test_rejection_beats_interview_words():
    body = "Thank you for interviewing with us. Unfortunately we will not be moving forward."
    assert classify("Interview update", body) == "rejected"


@pytest.mark.parametrize("current,suggested,ok", [
    ("new", "applied", True), ("ready", "screening", True), ("applied", "screening", True),
    ("screening", "applied", False), ("interviewing", "interviewing", False),
    ("interviewing", "offer", True), ("screening", "rejected", True),
    ("rejected", "interviewing", False), ("withdrawn", "rejected", False),
    ("offer", "rejected", True),
])
def test_should_suggest_only_forward_or_closing(current, suggested, ok):
    assert should_suggest(current, suggested) is ok


JOBS = [
    {"id": 1, "company": "Harvey", "title": "Research Engineer, Infrastructure", "status": "new"},
    {"id": 2, "company": "Harvey", "title": "Research Engineer / Scientist, Data & Evals", "status": "applied"},
    {"id": 3, "company": "Exa", "title": "Research, Evals", "status": "ready"},
    {"id": 4, "company": "Scale AI", "title": "Research Scientist", "status": "new"},
]


def test_match_by_sender_name_prefers_pipeline_job():
    j = match_job(JOBS, "Harvey Recruiting <no-reply@ashbyhq.com>", "Next steps", "phone screen")
    assert j["id"] == 2


def test_match_by_sender_domain():
    assert match_job(JOBS, "Jane <jane@exa.ai>", "Chat?", "")["id"] == 3


def test_match_title_in_body_wins():
    j = match_job(JOBS, "talent@harvey.ai", "Interview",
                  "About the Research Engineer, Infrastructure role")
    assert j["id"] == 1


def test_match_multiword_company_in_subject():
    assert match_job(JOBS, "x@greenhouse-mail.io", "Your Scale AI application", "")["id"] == 4


def test_company_only_in_body_is_not_a_match():
    assert match_job(JOBS, "news@example.com", "Weekly digest", "Harvey raised money") is None


def test_short_name_does_not_match_inside_words():
    assert match_job(JOBS, "team@hexagon.com", "Exam results", "") is None


def test_overlapping_names_pick_longest_and_unrelated_pair_gives_up():
    jobs = JOBS + [{"id": 5, "company": "Scale", "title": "Engineer", "status": "new"}]
    assert match_job(jobs, "x@y.io", "Scale AI interview", "")["id"] == 4
    assert match_job(JOBS, "x@y.io", "Harvey and Exa event", "") is None
