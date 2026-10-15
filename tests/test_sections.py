import zipfile
from pathlib import Path

from pdf2epub.cleanup import clean
from pdf2epub.model import Block, Book, Kind
from pdf2epub.sections import format_numeral, parse_numeral
from pdf2epub.structure import build_chapters
from pdf2epub.writer import write_epub

LONG = "טקסט ארוך של פרק בספר שממשיך ומשתרע על פני כמה שורות רצופות של הדפס. " * 4


def test_numerals_round_trip():
    assert parse_numeral("ב") == (2, "hebrew")
    assert parse_numeral("יא") == (11, "hebrew")
    assert parse_numeral("טו") == (15, "hebrew")
    assert parse_numeral("פרק ג") == (3, "hebrew")
    assert parse_numeral("IV") == (4, "roman")
    assert parse_numeral("Chapter 7") == (7, "arabic")
    assert parse_numeral("-") is None and parse_numeral("שלום") is None
    assert [format_numeral(n, "hebrew") for n in (1, 6, 11, 15, 16, 20)] == ["א", "ו", "יא", "טו", "טז", "כ"]
    assert format_numeral(14, "roman") == "XIV"


def _para(page, top, bottom, text=LONG, size=10.0, first=False):
    return Block(Kind.PARAGRAPH, text=text, page=page, top=top, box=(0.15, top, 0.85, bottom), size=size, first_on_page=first)


def _marker(page, top, text):
    return Block(Kind.PARAGRAPH, text=text, page=page, top=top, box=(0.495, top, 0.51, top + 0.012), size=10.0)


def _novel() -> list[Block]:
    """Part title page, then chapters marked by small centred numerals mid-page -- one misread ('-'),
    one dropped entirely (only the white space is left), the first one marked only by a low page start."""
    blocks = [Block(Kind.PARAGRAPH, text="הזר", page=1, top=0.15, box=(0.48, 0.15, 0.53, 0.17), size=13.0, first_on_page=True)]
    blocks += [_para(2, 0.36, 0.6, first=True), _para(2, 0.62, 0.9)]  # chapter 1: low page start
    blocks += [_para(3, 0.08, 0.3, first=True), _marker(3, 0.39, "ב"), _para(3, 0.42, 0.9)]
    blocks += [_para(4, 0.08, 0.3, first=True), _para(4, 0.43, 0.9)]  # chapter 3: marker dropped, gap left
    blocks += [_para(5, 0.08, 0.3, first=True), _marker(5, 0.39, "-"), _para(5, 0.42, 0.9)]  # chapter 4: misread
    blocks += [_para(6, 0.08, 0.3, first=True), _marker(6, 0.39, "ה"), _para(6, 0.42, 0.9)]
    return blocks


def test_numbered_chapters_found_and_renumbered():
    chapters = build_chapters(clean(_novel(), 6))
    toc = [(c.level, c.title) for c in chapters if c.in_toc]
    assert toc == [(1, "הזר"), (2, "א"), (2, "ב"), (2, "ג"), (2, "ד"), (2, "ה")]


def test_blank_page_restarts_numbering():
    blocks = _novel()
    blocks += [_marker(8, 0.34, "א"), _para(8, 0.37, 0.9)]  # page 7 is blank: part two
    blocks += [_para(9, 0.08, 0.3, first=True), _marker(9, 0.39, "ב"), _para(9, 0.42, 0.9)]
    chapters = build_chapters(clean(blocks, 9))
    assert [c.title for c in chapters if c.in_toc][-2:] == ["א", "ב"]


def test_display_pages_are_not_parts():
    blocks = []
    for page in range(1, 9):  # many pages with one big phrase each: display pages, not parts
        blocks.append(Block(Kind.PARAGRAPH, text="שורה גדולה", page=2 * page, top=0.4, box=(0.3, 0.4, 0.7, 0.45), size=20.0, first_on_page=True))
        blocks.append(_para(2 * page + 1, 0.08, 0.9, first=True))
    chapters = build_chapters(clean(blocks, 17))
    assert not any(c.in_toc for c in chapters)


def test_nested_toc_in_epub(tmp_path: Path):
    chapters = build_chapters(clean(_novel(), 6))
    out = tmp_path / "nested.epub"
    write_epub(Book(title="הזר", authors=[], language="he", direction="rtl", chapters=chapters), out)
    with zipfile.ZipFile(out) as z:
        nav = z.read("EPUB/nav.xhtml").decode()
    # the part's entry contains its own list of chapters
    part = nav.index(">הזר<")
    assert nav.index("<ol>", part) < nav.index(">א<") < nav.index(">ה<")


def test_contents_page_and_accessibility_metadata(tmp_path: Path):
    import re

    from pdf2epub.validate import check_epub, epubcheck_available

    front = [Block(Kind.PARAGRAPH, text="© 2005 כל הזכויות שמורות", page=0, first_on_page=True)]
    chapters = build_chapters(clean(front + _novel(), 6))
    out = tmp_path / "contents.epub"
    write_epub(Book(title="הזר", authors=["אלבר קאמי"], language="he", direction="rtl", chapters=chapters), out)
    with zipfile.ZipFile(out) as z:
        opf = z.read("EPUB/content.opf").decode()
        contents = z.read("EPUB/text/contents.xhtml").decode()
    assert "תוכן העניינים" in contents and 'href="ch' in contents and contents.count("<ol>") == 2
    spine = re.findall(r'<itemref idref="([^"]+)"', opf)
    assert spine.index("contents") == spine.index("ch001") + 1  # after the front matter, before the first part
    assert 'property="schema:accessibilityFeature">tableOfContents' in opf
    assert 'type="toc"' in opf and "text/contents.xhtml" in opf
    if epubcheck_available():
        result = check_epub(out)
        assert result is not None and result.ok, result.errors
