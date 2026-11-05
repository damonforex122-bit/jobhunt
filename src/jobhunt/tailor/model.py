"""Structured resume: what Gemini reads and what the renderer draws."""

from pydantic import BaseModel


class Run(BaseModel):
    text: str
    bold: bool = False
    italic: bool = False


class Link(BaseModel):
    label: str
    url: str


class Header(BaseModel):
    name: str
    contact: list[str]  # plain parts, e.g. ["San Francisco, CA", "me@x.com"]
    links: list[Link]


class Bullet(BaseModel):
    id: str  # "e{entry}b{bullet}", 0-based
    text: str


class Entry(BaseModel):
    id: str  # "e{n}"
    org: str
    location: str
    title: str
    dates: str
    bullets: list[Bullet]
    blurb: str = ""  # optional one-line description under the title (the master has these)


class SkillCategory(BaseModel):
    id: str  # "s{n}"
    name: str
    items: list[str]


class EducationLine(BaseModel):
    school: str
    detail: str
    dates: str


class Resume(BaseModel):
    header: Header
    summary: str
    experience: list[Entry]
    publications: list[list[Run]]
    skills: list[SkillCategory]
    education: list[EducationLine]

    def all_bullets(self) -> dict[str, Bullet]:
        return {b.id: b for e in self.experience for b in e.bullets}

    def full_text(self) -> str:
        h = self.header
        parts: list[str] = [h.name, *h.contact, *(link.label for link in h.links), self.summary]
        for e in self.experience:
            parts += [e.org, e.location, e.title, e.dates, e.blurb, *(b.text for b in e.bullets)]
        parts += ["".join(r.text for r in pub) for pub in self.publications]
        for s in self.skills:
            parts += [s.name, *s.items]
        for ed in self.education:
            parts += [ed.school, ed.detail, ed.dates]
        return "\n".join(p for p in parts if p)
