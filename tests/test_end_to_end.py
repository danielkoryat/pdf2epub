"""End-to-end conversions of small generated PDFs (runs the Docling models; marked slow)."""

import re
import zipfile
from pathlib import Path

import pymupdf
import pytest

from pdf2epub.pipeline import Options, convert
from pdf2epub.validate import check_epub, epubcheck_available

pytestmark = pytest.mark.slow

LOREM = (
    "It was a bright cold day in April, and the clocks were striking thirteen. The hallway smelt of boiled "
    "cabbage and old rag mats. At one end of it a coloured poster, too large for indoor display, had been "
    "tacked to the wall. It depicted simply an enormous face, more than a metre wide."
)
HEBREW = (
    "היה יום בהיר וקר באפריל, והשעונים צלצלו שלוש עשרה. בפרוזדור עמד ריח של כרוב מבושל ושל שטיחים ישנים. "
    "בקצהו האחד הוצמדה לקיר כרזה צבעונית, גדולה מדי לתצוגה בבית. היא תיארה פשוט פרצוף עצום, רחב יותר ממטר."
)


def _make_pdf(path: Path, title: str, chapters: list[tuple[str, list[str]]], rtl: bool) -> None:
    """A small book: title page, then each chapter starting on a new page with a footnote on the first."""
    doc = pymupdf.open()
    direction = ' dir="rtl"' if rtl else ""
    css = "body{font-family:sans-serif;} h1{font-size:28px;text-align:center;} p{font-size:13px;text-indent:20px;margin:0 0 6px 0;}"
    page = doc.new_page(width=420, height=595)
    page.insert_htmlbox(pymupdf.Rect(40, 200, 380, 400), f"<div{direction}><h1>{title}</h1></div>", css=css)
    for n, (heading, paragraphs) in enumerate(chapters):
        page = doc.new_page(width=420, height=595)
        body = "".join(f"<p>{p}</p>" for p in paragraphs)
        if n == 0:
            body = body.replace("</p>", "*</p>", 1)
        page.insert_htmlbox(pymupdf.Rect(40, 50, 380, 520), f"<div{direction}><h1>{heading}</h1>{body}</div>", css=css)
        if n == 0:
            note = "* הערת שוליים לדוגמה." if rtl else "* An example footnote."
            page.insert_htmlbox(pymupdf.Rect(40, 540, 380, 580), f"<div{direction}><p style='font-size:9px'>{note}</p></div>")
        page.insert_text((205, 575), str(n + 2), fontsize=9)
    doc.save(path)


def _rasterise(src: Path, dst: Path, dpi: int = 150) -> None:
    """An image-only copy of a PDF, like a scan."""
    doc, out = pymupdf.open(src), pymupdf.open()
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, pixmap=pix)
    out.save(dst)


def _epub_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return " ".join(
            re.sub(r"<[^>]+>", " ", z.read(n).decode()) for n in sorted(z.namelist()) if n.startswith("EPUB/text/")
        )


def _assert_valid(path: Path) -> None:
    if epubcheck_available():
        result = check_epub(path)
        assert result is not None and result.ok, result.errors


@pytest.fixture(scope="module")
def english_pdf(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("pdf") / "english.pdf"
    _make_pdf(path, "The Test Book", [("Chapter One", [LOREM, LOREM]), ("Chapter Two", [LOREM])], rtl=False)
    return path


@pytest.fixture(scope="module")
def hebrew_pdf(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("pdf") / "hebrew.pdf"
    _make_pdf(path, "ספר הבדיקה", [("פרק ראשון", [HEBREW, HEBREW]), ("פרק שני", [HEBREW])], rtl=True)
    return path


def test_born_digital_english(english_pdf: Path, tmp_path: Path):
    out = tmp_path / "en.epub"
    report = convert(english_pdf, out, Options(cover=False))
    assert report.scanned_pages == 0
    assert (report.language, report.direction) == ("en", "ltr")
    assert report.toc_entries == 2
    assert report.footnotes_linked == 1
    text = _epub_text(out)
    assert "clocks were striking thirteen" in text
    _assert_valid(out)


def test_born_digital_hebrew(hebrew_pdf: Path, tmp_path: Path):
    out = tmp_path / "he.epub"
    report = convert(hebrew_pdf, out, Options(cover=False))
    assert (report.language, report.direction) == ("he", "rtl")
    assert report.toc_entries == 2
    text = _epub_text(out)
    assert "השעונים צלצלו שלוש עשרה" in text  # logical order, not reversed
    _assert_valid(out)


def test_scanned_hebrew_is_ocrd(hebrew_pdf: Path, tmp_path: Path):
    scanned = tmp_path / "scan.pdf"
    _rasterise(hebrew_pdf, scanned)
    out = tmp_path / "scan.epub"
    report = convert(scanned, out, Options(cover=False))
    assert report.scanned_pages == report.pages
    assert report.ocr_lang == "heb"
    assert (report.language, report.direction) == ("he", "rtl")
    text = _epub_text(out)
    words = HEBREW.split()
    found = sum(w in text for w in words)
    assert found / len(words) > 0.9, f"only {found}/{len(words)} words recognised"
    _assert_valid(out)
