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
    for phrase in ("Accomplished [X] as measured by [Y], by doing [Z]", "no new numbers",
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
