import pypdf

from jobhunt.tailor import render
from jobhunt.tailor.model import Bullet
from jobhunt.tailor.render import fit_to_pages, render_pdf
from tailor_fakes import sample_resume


def test_render_sample_two_pages_or_less(tmp_path):
    p = tmp_path / "r.pdf"
    assert render_pdf(sample_resume(), p) <= 2
    box = pypdf.PdfReader(p).pages[0].mediabox
    assert [float(x) for x in box] == [0, 0, 612, 792]


def test_render_keeps_links(tmp_path):
    p = tmp_path / "r.pdf"
    resume = sample_resume()
    render_pdf(resume, p)
    annots = pypdf.PdfReader(p).pages[0].get("/Annots") or []
    uris = {a.get_object()["/A"]["/URI"] for a in annots if "/A" in a.get_object()}
    for link in resume.header.links:
        assert link.url in uris


def _special(tmp_path):
    resume = sample_resume()
    resume.experience[0].bullets[0].text = "Cut cost 40% – R&D <fast> café’s ●"
    p = tmp_path / "s.pdf"
    render_pdf(resume, p)
    text = "".join(pg.extract_text() for pg in pypdf.PdfReader(p).pages)
    assert "R&D <fast>" in text
    assert "café" in text


def test_render_special_characters(tmp_path, monkeypatch):
    _special(tmp_path)
    fb = tmp_path / "nofonts"
    fb.mkdir()
    monkeypatch.setattr(render, "FONT_DIR", fb)
    _special(fb)


def test_fit_to_pages_cuts_lowest_ranked_first(tmp_path):
    resume = sample_resume()
    pad = [Bullet(id=f"e0b{i}", text=f"Padding bullet number {i} " + "word " * 30) for i in range(4, 44)]
    resume.experience[0].bullets += pad
    real = [b for b in resume.all_bullets() if b not in {x.id for x in pad}]
    ranking = real + [b.id for b in pad]
    fitted, cut, n = fit_to_pages(resume, ranking, tmp_path / "f.pdf")
    assert n <= 2
    assert render_pdf(fitted, tmp_path / "g.pdf") <= 2
    assert cut
    tail = set(ranking[-len(cut):])
    assert set(cut) == tail
    assert all(len(e.bullets) >= 1 for e in fitted.experience)


def test_fit_to_pages_reports_pages_when_it_cannot_fit(tmp_path):
    resume = sample_resume()
    for e in resume.experience:
        e.bullets = e.bullets[:1]
    resume.summary = "word " * 3000
    _, cut, n = fit_to_pages(resume, [], tmp_path / "f.pdf")
    assert n > 2 and cut == []
