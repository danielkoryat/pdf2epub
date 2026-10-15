import zipfile
from pathlib import Path

import pytest

from pdf2epub.cleanup import clean
from pdf2epub.extract import make_chunks
from pdf2epub.lang import base_direction, direction_for_language, dominant_script, looks_visually_reversed
from pdf2epub.model import NOTE_OPEN, Block, Book, Chapter, Kind
from pdf2epub.ocr import group_paragraphs, parse_tsv
from pdf2epub.spell import Corrector
from pdf2epub.structure import build_chapters
from pdf2epub.validate import check_epub, epubcheck_available
from pdf2epub.writer import write_epub


def para(text, page, first=False, top=0.5, size=0.0):
    return Block(Kind.PARAGRAPH, text=text, page=page, first_on_page=first, top=top, size=size)


def heading(text, page, first=True, top=0.1, size=0.0):
    return Block(Kind.HEADING, text=text, page=page, first_on_page=first, top=top, size=size)


# --- language -------------------------------------------------------------------------------------------


def test_direction_and_script():
    assert base_direction("שלום world") == "rtl"
    assert base_direction("123 hello שלום") == "ltr"
    assert dominant_script("שלום לכולם, hi") == "HEBREW"
    assert direction_for_language("he") == "rtl"
    assert direction_for_language("ar-EG") == "rtl"
    assert direction_for_language("en") == "ltr"


def test_visual_order_hebrew_detected():
    logical = "שלום לכולם מה נשמע היום אנחנו הולכים לים עם כל החברים שלנו ושם נשחק כדור עד הערב"
    assert not looks_visually_reversed(logical)
    assert looks_visually_reversed(logical[::-1])


# --- clean-up -------------------------------------------------------------------------------------------


def test_paragraph_cut_by_page_break_is_joined():
    blocks = [para("The quick brown fox jumps over", 1, first=True), para("the lazy dog.", 2, first=True)]
    out = clean(blocks, 2)
    assert [b.text for b in out] == ["The quick brown fox jumps over the lazy dog."]
    assert out[0].pages == [1, 2]


def test_complete_paragraph_is_not_joined():
    blocks = [para("First paragraph ends here.", 1, first=True), para("Second one starts.", 2, first=True)]
    assert len(clean(blocks, 2)) == 2


def test_hyphenated_word_across_pages():
    blocks = [para("an exam-", 1, first=True), para("ple of hyphenation.", 2, first=True)]
    assert clean(blocks, 2)[0].text == "an example of hyphenation."


def test_page_numbers_and_noise_dropped():
    blocks = [
        para("Real text.", 1, first=True),
        para("14", 1, top=0.95),
        para("-.", 1, top=0.0),
        para("טו\\י\\,/", 1, top=0.1),
        para("* * *", 1),
    ]
    assert [b.text for b in clean(blocks, 1)] == ["Real text.", "* * *"]


def test_footnote_is_linked_to_marker_on_same_page():
    blocks = [
        para("אני עדיין מקבל מגפיים כבדים,* ולפעמים זה עוזר.", 5, first=True),
        Block(Kind.FOOTNOTE, text="* מגפיים כבדים - ביטוי לדכדוך.", page=5, top=0.9),
    ]
    out = clean(blocks, 5)
    note = next(b for b in out if b.kind is Kind.FOOTNOTE)
    assert note.linked and note.marker == "*"
    assert note.text == "מגפיים כבדים - ביטוי לדכדוך."
    assert f"כבדים,{NOTE_OPEN}{note.note_id}" in out[0].text


def test_numeric_footnote_not_matched_to_free_standing_number():
    blocks = [
        para("In 1 day he walked home.3 Then he slept.", 2, first=True),
        Block(Kind.FOOTNOTE, text="3 A long walk.", page=2, top=0.9),
    ]
    out = clean(blocks, 2)
    assert "In 1 day" in out[0].text  # the free-standing "1" was not touched
    assert f"home.{NOTE_OPEN}" in out[0].text


def test_repeated_running_header_dropped():
    blocks = []
    for page in range(1, 11):
        blocks.append(para("THE BOOK TITLE", page, first=True, top=0.03))
        blocks.append(para(f"Text of page {page}.", page))
    out = clean(blocks, 10)
    assert all(b.text != "THE BOOK TITLE" for b in out)
    assert len(out) == 10


# --- structure ------------------------------------------------------------------------------------------


def test_book_mode_chapters_and_toc():
    body = "Some long chapter text. " * 20
    blocks = [
        para("Title page", 1, first=True),
        para("For my family", 2, first=True),
        heading("Chapter One", 3, size=30),
        para(body, 3, size=20),
        heading("A sign on the wall", 4, first=False, top=0.5, size=30),  # mid-page: not a chapter
        para(body, 4, size=20),
        heading("Chapter Two", 5, size=30),
        para(body, 5, size=20),
        heading("Big display line", 6, size=30),  # heading-only page: own file, not in TOC
    ]
    chapters = build_chapters(blocks)
    assert [c.title for c in chapters if c.in_toc] == ["Chapter One", "Chapter Two"]
    assert chapters[0].in_toc is False and chapters[1].in_toc is False  # front matter, one page per file
    assert chapters[-1].title == "Big display line" and not chapters[-1].in_toc


def test_body_size_heading_does_not_start_chapter():
    body = "Text. " * 50
    blocks = [heading("Chapter", 1, size=30), para(body, 1, size=20), heading("Bold line", 2, size=20), para(body, 2, size=20)]
    assert [c.title for c in build_chapters(blocks)] == ["Chapter"]


def test_footnotes_move_to_chapter_notes():
    blocks = [heading("Ch", 1), para("Text. " * 50, 1), Block(Kind.FOOTNOTE, text="note", page=1, top=0.9)]
    (chapter,) = build_chapters(blocks)
    assert [n.text for n in chapter.notes] == ["note"]
    assert all(b.kind is not Kind.FOOTNOTE for b in chapter.blocks)


# --- OCR helpers ----------------------------------------------------------------------------------------


def test_make_chunks_splits_on_kind_and_size():
    kinds = ["digital"] * 3 + ["scanned"] * 5 + ["digital"]
    chunks = make_chunks(kinds, size=3)
    assert [(c.first, c.last, c.scanned) for c in chunks] == [(1, 3, False), (4, 6, True), (7, 8, True), (9, 9, False)]


TSV = "\n".join(
    [
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
        "5\t1\t1\t1\t1\t1\t200\t10\t50\t20\t95\tשלום",
        "5\t1\t1\t1\t1\t2\t100\t10\t80\t20\t90\tעולם",
        "5\t1\t1\t2\t1\t1\t210\t40\t40\t20\t80\tחדש",
    ]
)


def test_parse_tsv_and_group_paragraphs():
    words = parse_tsv(TSV)
    assert [w.text for w in words] == ["שלום", "עולם", "חדש"]
    paras = group_paragraphs(words, zoom=2.0, offset=(0.0, 0.0))
    assert [p.text for p in paras] == ["שלום עולם", "חדש"]
    assert paras[0].bbox == (50.0, 5.0, 125.0, 15.0)
    assert paras[0].size == 10.0


# --- spelling -------------------------------------------------------------------------------------------


def test_corrector_fixes_only_non_words():
    freqs = {"ידעתי": 5000, "עדיין": 9000, "עד": 100000, "ער": 3000, "יודע": 600000, "יורע": 38}
    c = Corrector("he", freqs)
    assert c.fix('ירעתי "עריין" ער') == 'ידעתי "עדיין" ער'  # ער is a real word: kept
    assert c.fix("יורע") == "יודע"  # a rare typo dwarfed by the real word
    assert c.fix("ג'ירג'יטסו בע\"מ") == "ג'ירג'יטסו בע\"מ"  # words with quotes inside are left alone


def test_corrector_disabled_for_languages_without_rules():
    assert Corrector("en", {"the": 5}).fix("tbe") == "tbe"


# --- writer ---------------------------------------------------------------------------------------------


def _sample_book(direction="rtl", language="he") -> Book:
    note = Block(Kind.FOOTNOTE, text="הערה", page=1, note_id="note-1", marker="*", linked=True)
    chapter = Chapter(
        title="פרק ראשון",
        blocks=[
            heading("פרק ראשון", 1),
            para(f"טקסט עם הערה{NOTE_OPEN}note-1 ועוד.", 1),
            para("English sentence inside.", 1),
            para("* * *", 1),
        ],
        notes=[note],
    )
    return Book(title="ספר", authors=["כותב"], language=language, direction=direction, chapters=[chapter])


def test_writer_rtl_metadata_and_footnotes(tmp_path: Path):
    out = tmp_path / "book.epub"
    write_epub(_sample_book(), out)
    with zipfile.ZipFile(out) as z:
        opf = z.read("EPUB/content.opf").decode()
        chapter = z.read("EPUB/text/ch001.xhtml").decode()
        nav = z.read("EPUB/nav.xhtml").decode()
    assert 'page-progression-direction="rtl"' in opf
    assert "primary-writing-mode" in opf and "<dc:language>he</dc:language>" in opf
    assert 'dir="rtl"' in chapter and 'lang="he"' in chapter
    assert 'href="../styles/book.css"' in chapter
    assert 'epub:type="noteref"' in chapter and 'id="note-1"' in chapter and 'href="#ref-note-1"' in chapter
    assert '<p dir="ltr">English sentence inside.</p>' in chapter
    assert 'class="scene-break"' in chapter
    assert "פרק ראשון" in nav


@pytest.mark.skipif(not epubcheck_available(), reason="EPUBCheck not installed")
def test_writer_output_passes_epubcheck(tmp_path: Path):
    for direction, language in (("rtl", "he"), ("ltr", "en")):
        out = tmp_path / f"{language}.epub"
        write_epub(_sample_book(direction, language), out)
        result = check_epub(out)
        assert result is not None and result.ok, result.errors


def test_copyright_page_not_in_toc():
    body = "Some long chapter text. " * 20
    blocks = [
        heading("EXTREMELY LOUD", 1, size=30),
        para("Copyright © 2005 by the author. All rights reserved. " * 3, 1, size=20),
        heading("Chapter One", 2, size=30),
        para(body, 2, size=20),
    ]
    assert [c.title for c in build_chapters(blocks) if c.in_toc] == ["Chapter One"]


def test_invisible_ocr_layer_over_scan_is_reocrd():
    import pymupdf

    from pdf2epub.analyze import classify_pages

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 300), False)
    pix.clear_with(230)
    doc = pymupdf.open()
    for render_mode in (3, 0):  # invisible OCR layer, then ordinary visible text
        page = doc.new_page(width=400, height=600)
        page.insert_image(page.rect, pixmap=pix)
        page.insert_text((40, 100), "An old OCR layer that sits on top of the scanned image. " * 2, fontsize=8, render_mode=render_mode)
    kinds = [p.kind for p in classify_pages(doc)]
    assert kinds == ["scanned", "digital"]


def test_corrector_keeps_real_words_with_prefixes():
    freqs = {"חצרות": 400, "והצרות": 300, "עדיין": 9000}
    c = Corrector("he", freqs)
    assert c.fix("וחצרות") == "וחצרות"  # ו + חצרות is a real word
    assert c.fix("עריין") == "עדיין"


def test_dash_styled_page_numbers_and_scene_breaks():
    dash = "—"
    blocks = [
        para("Text.", 1, first=True),
        para(f"{dash} 14 {dash}", 1, top=0.95),  # page number printed between dashes: dropped
        para(dash * 3, 1),  # scene break: kept
        para("2014", 1),  # a year is not a scene break
    ]
    out = clean(blocks, 1)
    assert [b.text for b in out] == ["Text.", dash * 3, "2014"]
    from pdf2epub.cleanup import SCENE_BREAK

    assert SCENE_BREAK.match(dash * 3) and not SCENE_BREAK.match("2014")
