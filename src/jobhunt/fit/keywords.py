"""Local keyword extraction and resume/posting keyword overlap."""
import re
from collections import Counter

TECH_TERMS = {
    # languages / data
    "python", "sql", "typescript", "javascript", "java", "go", "rust", "scala", "r",
    "pandas", "numpy", "spark", "airflow", "dbt", "snowflake", "databricks", "bigquery",
    "postgres", "etl", "data pipelines",
    # cloud / infra
    "aws", "gcp", "azure", "docker", "kubernetes", "terraform", "ci/cd", "fastapi", "flask",
    "react", "api", "rest", "graphql",
    # ML / LLM tools
    "pytorch", "tensorflow", "scikit-learn", "hugging face", "transformers", "langchain",
    "llamaindex", "openai", "anthropic", "gemini", "claude", "vllm", "mlflow", "wandb",
    "vector database", "embeddings", "pinecone",
    # ML / LLM methods
    "llm", "llms", "rag", "fine-tuning", "prompt engineering", "llm evaluation", "evals",
    "evaluation", "benchmark", "benchmarks", "benchmarking", "inter-rater agreement",
    "annotation", "labeling", "human data", "data quality", "rlhf", "rlaif", "dpo",
    "structured extraction", "information extraction", "nlp", "machine learning",
    "deep learning", "computer vision", "agents", "tool use", "red teaming", "safety",
    "alignment", "reward model", "synthetic data", "data curation", "a/b testing",
    "experimentation", "statistics", "applied research", "research",
}

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "at", "by", "from",
    "as", "is", "are", "was", "were", "be", "been", "being", "we", "you", "our", "your",
    "will", "can", "this", "that", "these", "those", "it", "its", "not", "but", "if", "so",
    "such", "their", "they", "who", "what", "which", "about", "into", "more", "other",
    "than", "also", "any", "all", "have", "has", "had", "do", "does", "us", "my", "i",
    "must", "should", "would", "could", "may", "etc", "e", "g", "per", "via", "within",
    "across", "strong", "ability", "experience", "work", "working", "team", "role",
}

_TOKEN_RE = re.compile(r"[a-z][a-z0-9+#]*")


def _term_regex(term: str) -> re.Pattern:
    body = re.escape(term).replace(r"\-", "[- ]").replace(r"\ ", r"[- ]")
    return re.compile(rf"(?<![a-z0-9+#]){body}(?![a-z0-9+#])")


_TECH_RES = {t: _term_regex(t) for t in TECH_TERMS}


def extract_keywords(text: str, top_n: int = 40) -> list[str]:
    text = text.lower()
    counts: Counter[str] = Counter()
    first: dict[str, int] = {}
    for term, rx in _TECH_RES.items():
        ms = list(rx.finditer(text))
        if ms:
            counts[term] = len(ms)
            first[term] = ms[0].start()
    tokens = [(m.group(), m.start()) for m in _TOKEN_RE.finditer(text)]
    bigrams: Counter[str] = Counter()
    for (a, pos), (b, _) in zip(tokens, tokens[1:]):
        if a in STOPWORDS or b in STOPWORDS or len(a) < 2 or len(b) < 2:
            continue
        bg = f"{a} {b}"
        bigrams[bg] += 1
        first.setdefault(bg, pos)
    for bg, n in bigrams.items():
        # Skip bigrams already covered by (or overlapping) a curated term.
        if n >= 2 and bg not in counts:
            counts[bg] = n
    ranked = sorted(counts, key=lambda t: (-counts[t], first[t]))
    return ranked[:top_n]


def keyword_fit(resume: str, posting: str) -> tuple[int, list[str]]:
    kws = extract_keywords(posting)
    if not kws:
        return 0, []
    resume_l = resume.lower()
    have = [k for k in kws if _term_regex(k).search(resume_l)]
    missing = [k for k in kws if k not in set(have)]
    return round(100 * len(have) / len(kws)), missing[:10]
