from pdf2epub.cleanup import clean
from pdf2epub.known import Declared, normalise, outline_entries, similar
from pdf2epub.model import Block, Kind
from pdf2epub.structure import build_chapters

TEXT = "Long paragraph of chapter text that goes on and on over several printed lines of the page. " * 4


def p(page, text=TEXT, top=0.3, first=False, kind=Kind.PARAGRAPH, size=10.0):
    return Block(kind, text=text, page=page, top=top, first_on_page=first, size=size)


def test_normalise_and_similar():
    assert normalise("שיבה לטיפַּזֶה!") == "שיבה לטיפזה"
    assert similar("Chapter One: The Start", "chapter one the start")
    assert similar("אחרית דבר", "אחרית דבר:")
    assert not similar("The End", "A Beginning")


def test_outline_entries_respect_page_range():
    toc = [[1, "Part One", 3], [2, "Chapter 1", 4], [2, "Chapter 2", 9], [1, "Appendix", 40]]
    got = outline_entries(toc, first_page=3, page_count=10)
    assert [(d.title, d.page, d.level) for d in got] == [("Part One", 1, 1), ("Chapter 1", 2, 2), ("Chapter 2", 7, 2)]


def test_bookmarks_override_guessed_headings():
    blocks = [
        p(1, "Chapter 1", top=0.1, first=True, kind=Kind.HEADING, size=20), p(1),
        p(2, "A SIGN ON THE WALL", top=0.08, first=True, kind=Kind.HEADING, size=20), p(2),  # not a chapter
        p(3, "Chapter 2", top=0.1, first=True, kind=Kind.PARAGRAPH, size=10), p(3),  # missed by layout
        p(4, top=0.08, first=True), p(4),  # bookmark target page has no heading at all
    ]
    outline = [Declared("Chapter 1", 1), Declared("Chapter 2", 3), Declared("Epilogue", 4)]
    chapters = build_chapters(clean(blocks, 4, outline))
    assert [c.title for c in chapters if c.in_toc] == ["Chapter 1", "Chapter 2", "Epilogue"]
    sign = next(b for c in chapters for b in c.blocks if b.text == "A SIGN ON THE WALL")
    assert sign.level >= 3  # kept as a heading inside the chapter


def test_printed_contents_page_with_page_numbers():
    blocks = [p(1, "My Book", top=0.3, first=True)]
    blocks += [p(2, "Contents", top=0.1, first=True), p(2, "The Beginning 1", top=0.2),
               p(2, "In The Middle 3", top=0.25), p(2, "The Ending 5", top=0.3), p(2, "Notes 7", top=0.35)]
    for page in range(3, 11):  # printed page n is PDF page n + 2, numbers printed at the bottom
        blocks.append(p(page, first=True, top=0.08))
        blocks.append(Block(Kind.PARAGRAPH, text=str(page - 2), page=page, top=0.95, marker="folio"))
    # chapter titles on printed pages 1, 3, 5 (= PDF pages 3, 5, 7), set as ordinary lines
    blocks.insert(blocks.index(next(b for b in blocks if b.page == 3)), p(3, "The Beginning", top=0.05))
    blocks.insert(blocks.index(next(b for b in blocks if b.page == 5)), p(5, "In The Middle", top=0.05))
    blocks.insert(blocks.index(next(b for b in blocks if b.page == 7)), p(7, "The Ending", top=0.05))
    chapters = build_chapters(clean(blocks, 10))
    toc = [c.title for c in chapters if c.in_toc]
    assert toc == ["The Beginning", "In The Middle", "The Ending"]
    assert not any("Contents" == b.text for c in chapters for b in c.blocks)  # printed contents page replaced


def test_bio_with_years_is_not_a_contents_page():
    blocks = [p(1, "Born in 1913, he wrote The Stranger 1942 and The Plague 1947 and The Fall 1956.", first=True)]
    blocks += [p(page, first=True, top=0.08) for page in range(2, 12)]
    chapters = build_chapters(clean(blocks, 11))
    assert not any(b.marker == "known" for c in chapters for b in c.blocks)
