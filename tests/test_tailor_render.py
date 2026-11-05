import pypdf
import pytest

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
    monkeypatch.setattr(render, "CARLITO_PATH", fb / "missing.ttc")
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


# --- reference layout -------------------------------------------------------------------------

def _lines(path, page=0):
    """(baseline y from top, x, font name, size, text) for each text run on a page."""
    out = []

    def visit(text, cm, tm, font, size):
        if text.strip():
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            out.append((792 - y, x, (font or {}).get("/BaseFont", ""), size, text.strip()))

    pypdf.PdfReader(path).pages[page].extract_text(visitor_text=visit)
    return out


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "r.pdf"
    render_pdf(sample_resume(), p)
    return p


def test_fonts_are_carlito(pdf):
    names = set()
    for pg in pypdf.PdfReader(pdf).pages:
        for f in pg["/Resources"]["/Font"].values():
            names.add(f.get_object()["/BaseFont"])
    joined = " ".join(names)
    assert "Carlito-Bold" in joined
    assert "Carlito-Italic" in joined  # the sample has a blurb
    assert any(n.split("+")[-1].startswith("Carlito-") and "Bold" not in n and "Italic" not in n for n in names)


def test_heading_geometry(pdf):
    rows = _lines(pdf)
    i = next(n for n, r in enumerate(rows) if r[4] == "SUMMARY")
    y, x, font, size, _ = rows[i]
    assert "Bold" in font and size == pytest.approx(11)
    assert x == pytest.approx(50.5, abs=1)
    assert rows[i + 1][0] - y == pytest.approx(16.3, abs=1)
    j = next(n for n, r in enumerate(rows) if r[4] == "EXPERIENCE")
    assert rows[j + 1][0] - rows[j][0] == pytest.approx(18.3, abs=1)
    assert rows[j][0] - rows[j - 1][0] == pytest.approx(19.2, abs=1)


def test_heading_has_rule(pdf):
    content = pypdf.PdfReader(pdf).pages[0].get_contents().get_data()
    assert content.count(b".75 w") == 5  # one rule per section


def test_dates_use_to(pdf):
    text = "".join(pg.extract_text() for pg in pypdf.PdfReader(pdf).pages)
    assert "Jan 2022 to Present" in text
    assert "\u2013" not in text


def test_contact_line_shows_full_urls(pdf):
    text = " ".join(r[4] for r in _lines(pdf)[:4])
    assert "https://example.com/alex" in text
    assert "https://github.example.com/alex" in text
    assert "Springfield, ST | alex@example.com" in text
    assert "Website" not in text


def test_skills_category_is_bold(pdf):
    rows = [r for pg in range(2) for r in _lines(pdf, pg)] if len(pypdf.PdfReader(pdf).pages) > 1 else _lines(pdf)
    cat = next(r for r in rows if r[4].startswith("Languages"))
    assert "Bold" in cat[2]
    assert cat[4] == "Languages"
    rest = rows[rows.index(cat) + 1]
    assert rest[4].startswith(": Python, SQL, Go") and "Bold" not in rest[2]


def test_publications_are_plain(pdf):
    rows = [r for pg in range(len(pypdf.PdfReader(pdf).pages)) for r in _lines(pdf, pg)]
    assert not any("Italic" in r[2] for r in rows if "Journal of Examples" in r[4])


def test_left_edge_and_bullet_text_x(pdf):
    rows = _lines(pdf)
    glyph = next(r for r in rows if r[4] == "\u2022")
    assert glyph[1] == pytest.approx(50.5, abs=1)
    body = next(r for r in rows if r[4].startswith("Built a demand forecasting"))
    assert body[1] == pytest.approx(68.5, abs=1)
    assert next(r for r in rows if r[4] == "SUMMARY")[1] == pytest.approx(50.5, abs=1)
