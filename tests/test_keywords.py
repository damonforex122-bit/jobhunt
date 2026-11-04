from jobhunt.fit.keywords import extract_keywords, keyword_fit


def test_keyword_fit_basic():
    resume = "python rag evals"
    posting = "We use Python daily. Experience with Databricks and AWS. You will build RAG systems."
    score, missing = keyword_fit(resume, posting)
    assert score == 50
    assert missing == ["databricks", "aws"]


def test_keyword_fit_empty_posting():
    assert keyword_fit("python", "") == (0, [])


def test_extract_keywords_ranked_by_frequency_and_bigrams():
    text = "sql sql sql python. data labeling pipelines; data labeling quality."
    kws = extract_keywords(text)
    assert kws[0] == "sql"
    assert "python" in kws
    assert "data labeling" in kws
    assert "the data" not in kws


def test_no_substring_match():
    assert extract_keywords("great storage and paragraph") == []


def test_missing_capped_at_ten():
    posting = " ".join(["python", "sql", "aws", "gcp", "azure", "docker", "kubernetes",
                        "pytorch", "tensorflow", "spark", "databricks", "airflow"])
    score, missing = keyword_fit("", posting)
    assert score == 0
    assert len(missing) == 10
