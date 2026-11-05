from tailor_fakes import sample_resume

from jobhunt.tailor.agents import Draft, DraftBullet, DraftEntry, DraftSkills, Verdict, Verdicts
from jobhunt.tailor.check import assemble, deterministic_problems

MASTER = sample_resume()


def all_ok(pairs):
    return Verdicts(verdicts=[Verdict(index=i, supported=True, reason="") for i in range(len(pairs))])


def clean_draft() -> Draft:
    return Draft(
        summary=MASTER.summary,
        entries=[
            DraftEntry(entry_id="e0", bullets=[
                DraftBullet(text="Built a forecasting service that reduced stockouts by 18% across 120 warehouses.", source_ids=["e0b0"]),
                DraftBullet(text="Cut batch latency from 6 hours to 20 minutes with a new event pipeline.", source_ids=["e0b1"]),
            ]),
            DraftEntry(entry_id="e1", bullets=[
                DraftBullet(text="Lifted retention by 4 points with churn models.", source_ids=["e1b0"]),
            ]),
            DraftEntry(entry_id="e2", bullets=[
                DraftBullet(text="Built a text-mining index of survey responses.", source_ids=["e2b0"]),
            ]),
        ],
        skills=[DraftSkills(category_id="s0", items=["Python", "sql"])],
        ranking=[2, 0, 1, 3],
    )


def one_bullet_draft(text, source_ids, entry_id="e0") -> Draft:
    d = clean_draft()
    d.entries = [DraftEntry(entry_id=entry_id, bullets=[DraftBullet(text=text, source_ids=source_ids)])]
    d.ranking = [0]
    return d


def test_clean_draft_passes_through_unchanged():
    d = clean_draft()
    assert deterministic_problems(d, MASTER) == {}
    resume, ranking, fallbacks = assemble(d, MASTER, all_ok)
    assert fallbacks == []
    texts = {b.id: b.text for b in resume.all_bullets().values()}
    assert texts["e0t0"] == d.entries[0].bullets[0].text
    assert texts["e1t0"] == d.entries[1].bullets[0].text
    assert ranking == ["e1t0", "e0t0", "e0t1", "e2t0"]
    assert resume.summary == MASTER.summary


def test_new_number_falls_back_to_source():
    d = one_bullet_draft("Lifted retention by 45% with churn models.", ["e1b0"], "e1")
    probs = deterministic_problems(d, MASTER)
    assert "b0" in probs
    resume, _, fallbacks = assemble(d, MASTER, all_ok)
    assert resume.experience[1].bullets[0].text == MASTER.experience[1].bullets[0].text
    mine = [f for f in fallbacks if "Lifted retention" in f]
    assert len(mine) == 1
    assert mine[0].startswith('Kept your original wording for "Lifted retention by 45%')
    assert "45" in mine[0]


def test_unknown_or_empty_source_ids_fall_back():
    for srcs in ([], ["e9b9"], ["summary"], ["s0"]):
        d = one_bullet_draft("Built things.", srcs)
        assert "b0" in deterministic_problems(d, MASTER)
        resume, _, fallbacks = assemble(d, MASTER, all_ok)
        assert resume.experience[0].bullets[0].text == MASTER.experience[0].bullets[0].text
        assert fallbacks


def test_bullet_with_source_from_other_entry_rejected():
    d = one_bullet_draft("Lifted retention by 4 points.", ["e1b0"], "e0")
    assert "b0" in deterministic_problems(d, MASTER)
    resume, _, _ = assemble(d, MASTER, all_ok)
    assert all("retention" not in b.text for b in resume.experience[0].bullets)


def test_new_proper_noun_rejected():
    d = one_bullet_draft("Deployed services using Kubernetes.", ["e0b1"])
    assert "Kubernetes" in deterministic_problems(d, MASTER)["b0"]
    resume, _, _ = assemble(d, MASTER, all_ok)
    assert resume.experience[0].bullets[0].text == MASTER.experience[0].bullets[1].text


def test_first_word_of_text_and_sentence_is_not_checked():
    d = one_bullet_draft("Kubernetes is not here. Terraform handled it.", ["e0b1"])
    assert "Kubernetes" not in deterministic_problems(d, MASTER).get("b0", "")


def test_capitalized_terms_match_whole_tokens():
    for text in ("Wrote it in R.", "Built it with Spring.", "Wrote it in Java."):
        assert "b0" in deterministic_problems(one_bullet_draft(text, ["e0b1"]), MASTER), text


def test_only_sentence_start_is_exempt():
    for text in (
        "Tools: Kubernetes and the pipeline.",
        "Designed it; Kubernetes managed it.",
        "Designed it, e.g. Kubernetes based.",
        "e.g. Kubernetes based.",
    ):
        assert "Kubernetes" in deterministic_problems(one_bullet_draft(text, ["e0b1"]), MASTER).get("b0", ""), text


def test_summary_goes_to_judge_and_rejection_falls_back():
    d = clean_draft()
    d.summary = "Engineer who ships data products."
    seen = []

    def judge(pairs):
        seen.append(pairs)
        return Verdicts(verdicts=[
            Verdict(index=i, supported=i != len(pairs) - 1, reason="overclaims") for i in range(len(pairs))
        ])

    resume, _, fallbacks = assemble(d, MASTER, judge)
    assert len(seen) == 1
    text, sources = seen[0][-1]
    assert text == d.summary and sources[0] == MASTER.summary and len(sources) == 1 + len(MASTER.all_bullets())
    assert resume.summary == MASTER.summary
    assert len(fallbacks) == 1 and "overclaims" in fallbacks[0]


def test_empty_bullet_fails_and_duplicate_verdicts_any_false_wins():
    assert "b0" in deterministic_problems(one_bullet_draft("  ", ["e0b1"]), MASTER)
    d = one_bullet_draft("Designed the event pipeline.", ["e0b1"])

    def judge(pairs):
        return Verdicts(verdicts=[Verdict(index=0, supported=True, reason=""), Verdict(index=0, supported=False, reason="no")]
                        + [Verdict(index=1, supported=True, reason="")])

    resume, _, fallbacks = assemble(d, MASTER, judge)
    assert any("(no)" in f for f in fallbacks)


def test_skill_items_use_master_spelling_and_dedupe():
    d = clean_draft()
    d.skills = [DraftSkills(category_id="s0", items=["python", "PYTHON", "Sql"])]
    resume, _, fallbacks = assemble(d, MASTER, all_ok)
    assert resume.skills[0].items == ["Python", "SQL"]
    assert fallbacks == []


def test_judge_rejection_falls_back_and_receives_only_survivors():
    d = clean_draft()
    d.entries[0].bullets[1] = DraftBullet(text="Cut latency using Kubernetes.", source_ids=["e0b1"])
    seen = []

    def judge(pairs):
        seen.append(pairs)
        return Verdicts(verdicts=[Verdict(index=0, supported=False, reason="overclaims"), Verdict(index=1, supported=True, reason=""), Verdict(index=2, supported=True, reason=""), Verdict(index=3, supported=True, reason="")])

    resume, _, fallbacks = assemble(d, MASTER, judge)
    assert len(seen) == 1 and len(seen[0]) == 4  # 3 surviving bullets + summary
    assert seen[0][0] == (d.entries[0].bullets[0].text, [MASTER.experience[0].bullets[0].text])
    assert resume.experience[0].bullets[0].text == MASTER.experience[0].bullets[0].text
    assert len(fallbacks) == 2
    assert any("overclaims" in f for f in fallbacks)


def test_missing_verdict_counts_as_unsupported():
    d = one_bullet_draft("Designed the event pipeline.", ["e0b1"])
    resume, _, fallbacks = assemble(d, MASTER, lambda pairs: Verdicts(verdicts=[]))
    assert resume.experience[0].bullets[0].text == MASTER.experience[0].bullets[1].text
    assert fallbacks


def test_fallback_does_not_duplicate_existing_bullet():
    d = one_bullet_draft("Bad 99% claim.", ["e0b1"])
    d.entries[0].bullets.append(DraftBullet(text="Again 77% claim.", source_ids=["e0b1"]))
    resume, _, _ = assemble(d, MASTER, all_ok)
    assert [b.text for b in resume.experience[0].bullets] == [MASTER.experience[0].bullets[1].text]


def test_summary_with_new_claim_falls_back_to_master_summary():
    for s in ("Engineer with 15 years of experience.", "Engineer fluent in Rust and Kubernetes."):
        d = clean_draft()
        d.summary = s
        assert "summary" in deterministic_problems(d, MASTER)
        resume, _, fallbacks = assemble(d, MASTER, all_ok)
        assert resume.summary == MASTER.summary
        assert len(fallbacks) == 1


def test_skill_not_in_master_category_drops_category_to_master():
    d = clean_draft()
    d.skills = [DraftSkills(category_id="s0", items=["Python", "Rust"]), DraftSkills(category_id="s9", items=["x"])]
    probs = deterministic_problems(d, MASTER)
    assert "ss0" in probs and "ss9" in probs
    resume, _, fallbacks = assemble(d, MASTER, all_ok)
    assert resume.skills[0].items == MASTER.skills[0].items
    assert [s.id for s in resume.skills] == ["s0", "s1", "s2"]
    assert resume.skills[1] == MASTER.skills[1]
    assert len(fallbacks) == 1


def test_entry_with_zero_bullets_gets_first_master_bullet():
    d = clean_draft()
    d.entries = [e for e in d.entries if e.entry_id != "e2"]
    d.entries[1].bullets = []
    d.ranking = [0, 1]
    resume, ranking, fallbacks = assemble(d, MASTER, all_ok)
    assert resume.experience[1].bullets[0].text == MASTER.experience[1].bullets[0].text
    assert resume.experience[2].bullets[0].text == MASTER.experience[2].bullets[0].text
    assert len(fallbacks) == 2
    assert set(ranking) == set(resume.all_bullets())
    assert ranking[:2] == ["e0t0", "e0t1"]


def test_entries_keep_master_org_title_dates_and_order():
    d = clean_draft()
    d.entries.reverse()
    d.ranking = [99, -1, 0]
    resume, ranking, _ = assemble(d, MASTER, all_ok)
    assert [e.id for e in resume.experience] == ["e0", "e1", "e2"]
    for got, want in zip(resume.experience, MASTER.experience):
        assert (got.org, got.location, got.title, got.dates, got.blurb) == (want.org, want.location, want.title, want.dates, want.blurb)
    assert resume.header == MASTER.header
    assert resume.publications == MASTER.publications
    assert resume.education == MASTER.education
    assert ranking[0] == "e2t0"
    assert sorted(ranking) == sorted(resume.all_bullets())
