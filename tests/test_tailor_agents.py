import pytest
from tailor_fakes import FakeGemini, sample_resume

from jobhunt.config import Settings
from jobhunt.tailor import agents
from jobhunt.tailor.agents import (
    AgentError, Draft, DraftBullet, DraftEntry, DraftSkills, Match, Matches,
    Requirement, Requirements, Verdict, Verdicts,
)

SECRET = "sekret-key-123"
MUST = "Experience with Kubernetes operators"
NICE = "Forecasting background"


def settings(key="k"):
    return Settings(gemini_api_key=key)


def reqs():
    return Requirements(requirements=[
        Requirement(id="r0", text=MUST, kind="must", phrases=["Kubernetes"]),
        Requirement(id="r1", text=NICE, kind="nice", phrases=["forecasting"]),
    ])


def matches():
    return Matches(matches=[
        Match(requirement_id="r0", evidence_ids=[]),
        Match(requirement_id="r1", evidence_ids=["e0b0"]),
    ])


def draft():
    return Draft(
        summary="s",
        entries=[DraftEntry(entry_id="e0", bullets=[DraftBullet(text="t", source_ids=["e0b0"])])],
        skills=[DraftSkills(category_id="s0", items=["Python"])],
        ranking=[0],
    )


def all_responses():
    return {
        Requirements: reqs(), Matches: matches(), Draft: draft(),
        Verdicts: Verdicts(verdicts=[Verdict(index=0, supported=True, reason="ok")]),
    }


def test_each_agent_requests_its_schema():
    client = FakeGemini(all_responses())
    s = settings()
    agents.read_posting(client, s, "posting", "Acme", "Eng")
    agents.match(client, s, sample_resume(), reqs())
    agents.write(client, s, sample_resume(), reqs(), matches())
    agents.judge(client, s, [("rewritten", ["source"])])
    assert [c["config"]["response_schema"] for c in client.calls] == [Requirements, Matches, Draft, Verdicts]
    for c in client.calls:
        assert c["model"] == s.gemini_model
        assert c["config"]["response_mime_type"] == "application/json"
        assert c["config"]["temperature"] == 0.2


def test_reader_asks_for_at_most_15():
    client = FakeGemini(all_responses())
    agents.read_posting(client, settings(), "posting body", "Acme", "Eng")
    p = client.calls[0]["contents"]
    assert "at most 15" in p and "posting body" in p and "Acme" in p


def test_writer_prompt_lists_only_master_material_and_ids():
    client = FakeGemini(all_responses())
    resume = sample_resume()
    agents.write(client, settings(), resume, reqs(), matches())
    p = client.calls[0]["contents"]
    for b in resume.all_bullets().values():
        assert b.id in p and b.text in p
    assert "Use only facts in the cited source bullets" in p
    gaps_at = p.index("Gaps (do not write about these)")
    assert MUST not in p[:gaps_at] and MUST in p[gaps_at:]
    assert p.count(MUST) == 1
    assert NICE in p[:gaps_at]


def test_writer_prompt_states_xyz_and_rules():
    client = FakeGemini(all_responses())
    agents.write(client, settings(), sample_resume(), reqs(), matches())
    p = client.calls[0]["contents"]
    for phrase in ("short, plain sentences that lead with what she did", "never use the template phrases", "no new numbers",
                   "keep category names", "use the posting's wording only where the source supports it",
                   "at least one source_id from the same entry", "flattened"):
        assert phrase in p


def test_matcher_prompt_has_master_ids():
    client = FakeGemini(all_responses())
    agents.match(client, settings(), sample_resume(), reqs())
    p = client.calls[0]["contents"]
    assert "e1b2" in p and "summary" in p and "s0:Python" in p and MUST in p


def test_judge_prompt_pairs_indexed():
    client = FakeGemini(all_responses())
    agents.judge(client, settings(), [("REWRITE-A", ["SRC-A1", "SRC-A2"])])
    p = client.calls[0]["contents"]
    assert "REWRITE-A" in p and "SRC-A1" in p and "SRC-A2" in p


def test_agent_error_on_bad_parse():
    with pytest.raises(AgentError):
        agents.read_posting(FakeGemini({Requirements: None}), settings(), "p", "c", "t")


def test_agent_error_masks_key():
    client = FakeGemini(exc=RuntimeError(f"boom {SECRET} " + "x" * 500))
    with pytest.raises(AgentError) as ei:
        agents.judge(client, settings(key=SECRET), [("a", ["b"])])
    assert SECRET not in str(ei.value) and "***" in str(ei.value)


def test_supported_requirements_validates_ids_and_last_match_wins():
    resume = sample_resume()
    rs = reqs()
    ok = Matches(matches=[Match(requirement_id="r0", evidence_ids=["e9b9"]),
                          Match(requirement_id="zz", evidence_ids=["e0b0"]),
                          Match(requirement_id="r1", evidence_ids=["e0b0"])])
    supported, gaps = agents.supported_requirements(resume, rs.requirements, ok)
    assert [r.id for r in supported] == ["r1"] and [r.id for r in gaps] == ["r0"]
    dup = Matches(matches=[Match(requirement_id="r1", evidence_ids=["e0b0"]),
                           Match(requirement_id="r1", evidence_ids=[])])
    supported, gaps = agents.supported_requirements(resume, rs.requirements, dup)
    assert supported == [] and len(gaps) == 2
    kinds = Matches(matches=[Match(requirement_id="r0", evidence_ids=["summary"]),
                             Match(requirement_id="r1", evidence_ids=["s0:python"])])
    supported, _ = agents.supported_requirements(resume, rs.requirements, kinds)
    assert [r.id for r in supported] == ["r0", "r1"]
    bad_skill = Matches(matches=[Match(requirement_id="r0", evidence_ids=["s0:Rust", "s7:Python"])])
    assert agents.supported_requirements(resume, rs.requirements, bad_skill)[0] == []


def test_writer_keeps_hallucinated_evidence_out_of_supported_block():
    client = FakeGemini(all_responses())
    bad = Matches(matches=[Match(requirement_id="r0", evidence_ids=["e9b9"]),
                           Match(requirement_id="r1", evidence_ids=["e0b0"])])
    agents.write(client, settings(), sample_resume(), reqs(), bad)
    p = client.calls[0]["contents"]
    gaps_at = p.index("Gaps (do not write about these)")
    assert MUST not in p[:gaps_at] and MUST in p[gaps_at:]


# --- time limit, retry, step names (a bare Gemini client is a one-provider chain) ---

class ReadTimeout(Exception):
    """Named like httpx's timeout errors, which is what the client raises."""


@pytest.fixture
def no_sleep(monkeypatch):
    from jobhunt.tailor import providers
    slept = []
    monkeypatch.setattr(providers, "_sleep", slept.append)
    return slept


def test_make_client_is_none_without_any_key():
    assert agents.make_client(Settings()) is None


def test_timeout_is_retried_once_then_succeeds(no_sleep):
    fake = FakeGemini({Requirements: reqs()}, exc=ReadTimeout("read timed out"), fail_times=1)
    out = agents.read_posting(fake, settings(), "posting", "Acme", "Engineer")
    assert out == reqs() and len(fake.calls) == 2 and len(no_sleep) == 1


def test_timeout_twice_names_the_step(no_sleep):
    from jobhunt.tailor.providers import CALL_TIMEOUT_S, RETRIES
    fake = FakeGemini({Draft: None}, exc=ReadTimeout("read timed out"))
    with pytest.raises(AgentError) as e:
        agents.write(fake, settings(), sample_resume(), reqs(), matches())
    assert str(e.value) == f"Writing the resume failed — Gemini: no answer within {CALL_TIMEOUT_S}s"
    assert len(fake.calls) == 1 + RETRIES


def test_overloaded_503_is_retried(no_sleep):
    from google.genai import errors
    exc = errors.ServerError(503, {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}})
    fake = FakeGemini({Matches: matches()}, exc=exc, fail_times=1)
    assert agents.match(fake, settings(), sample_resume(), reqs()) == matches()
    assert len(fake.calls) == 2


def test_other_errors_are_not_retried_and_name_the_step(no_sleep):
    fake = FakeGemini({}, exc=ValueError("bad request"))
    with pytest.raises(AgentError) as e:
        agents.judge(fake, settings(), [("text", ["source"])])
    assert len(fake.calls) == 1 and not no_sleep
    assert str(e.value).startswith("Checking the rewrites failed — Gemini: ")
