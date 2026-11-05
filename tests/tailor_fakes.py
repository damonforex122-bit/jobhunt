"""Synthetic resume data for tailor tests. Not a real person."""

from jobhunt.tailor.model import (
    Bullet,
    EducationLine,
    Entry,
    Header,
    Link,
    Resume,
    Run,
    SkillCategory,
)


def _entry(n: int, org: str, location: str, title: str, dates: str, texts: list[str], blurb: str = "") -> Entry:
    return Entry(
        id=f"e{n}",
        org=org,
        location=location,
        title=title,
        dates=dates,
        blurb=blurb,
        bullets=[Bullet(id=f"e{n}b{i}", text=t) for i, t in enumerate(texts)],
    )


def sample_resume() -> Resume:
    return Resume(
        header=Header(
            name="Alex Q. Sample, MSc",
            contact=["Springfield, ST", "alex@example.com"],
            links=[
                Link(label="Website", url="https://example.com/alex"),
                Link(label="LinkedIn", url="https://linkedin.example.com/in/alex"),
                Link(label="GitHub", url="https://github.example.com/alex"),
            ],
        ),
        summary=(
            "Engineer who ships data products end to end. Led a small team building forecasting "
            "tools for logistics customers, cut report turnaround from days to minutes, and "
            "mentored four junior analysts. Comfortable with ambiguity, stakeholders and code."
        ),
        experience=[
            _entry(
                0, "Widget Works", "Springfield, ST", "Staff Engineer", "Jan 2022 – Present",
                [
                    "Built a demand forecasting service that reduced stockouts by 18% across 120 warehouses and was adopted by three business units within a quarter.",
                    "Designed the event pipeline that processes 2M records a day, cutting batch latency from 6 hours to 20 minutes.",
                    "Mentored four engineers; two were promoted within 18 months.",
                    "Introduced contract tests between services, halving production incidents.",
                ],
                blurb="Platform team for the supply-chain analytics group.",
            ),
            _entry(
                1, "Gadget Labs", "Shelbyville, ST", "Data Scientist", "Mar 2019 – Dec 2021",
                [
                    "Trained churn models that lifted retention by 4 points on a base of 300K subscribers.",
                    "Wrote the experimentation guide adopted by the product org.",
                    "Partnered with finance to model pricing scenarios for the annual plan.",
                ],
            ),
            _entry(
                2, "Example University, Dept. of Systems", "Capital City, ST", "Research Assistant", "Aug 2015 – Feb 2019",
                [
                    "Built a text-mining index of survey responses using unsupervised learning.",
                    "Lead author on a journal paper analysing 50K operational records.",
                    "Taught a weekly lab for 40 undergraduates.",
                ],
            ),
        ],
        publications=[
            [Run(text="Sample A., Doe J. (2023). Forecasting under uncertainty. "), Run(text="Journal of Examples", italic=True), Run(text=", 12.")],
            [Run(text="Sample A. Notes on contract testing. Preprint 2401.00001.")],
        ],
        skills=[
            SkillCategory(id="s0", name="Languages", items=["Python", "SQL", "Go"]),
            SkillCategory(id="s1", name="ML", items=["forecasting", "causal inference", "experiment design"]),
            SkillCategory(id="s2", name="Tools", items=["Postgres", "Airflow", "Docker", "Terraform"]),
        ],
        education=[
            EducationLine(school="Example University", detail="PhD, Systems Engineering", dates="2015 – 2019"),
            EducationLine(school="Sample Institute", detail="BS, Mathematics", dates="2011 – 2015"),
        ],
    )


class FakeGemini:
    """Stand-in for genai.Client: returns a canned parsed object per response schema."""

    def __init__(self, responses: dict | None = None, exc: Exception | None = None):
        self.responses = responses or {}
        self.exc = exc
        self.calls: list[dict] = []
        self.models = self

    def generate_content(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        parsed = self.responses.get(kw["config"]["response_schema"])
        return type("R", (), {"parsed": parsed})()
