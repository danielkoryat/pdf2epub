"""Splits the cleaned block list into chapters (one XHTML file each) and collects their footnotes."""

from __future__ import annotations

import logging
import re

from .model import Block, Chapter, Kind

log = logging.getLogger(__name__)

# Untitled stretches of text are split into files of about this many blocks: Kindle gets sluggish with
# very large XHTML files.
MAX_BLOCKS_PER_FILE = 400
# A titled section needs at least this much body text to be listed in the table of contents.
MIN_CHAPTER_CHARS = 200
# A chapter title must be at least this much larger than body text (when text sizes are known).
MIN_TITLE_SCALE = 1.2
# A short section with any of these is the copyright page: part of the book, but not a contents entry.
_COPYRIGHT = re.compile(r"©|\bISBN\b|all rights reserved|כל הזכויות שמורות|tous droits réservés|alle rechte vorbehalten", re.IGNORECASE)


def median_body_size(blocks: list[Block]) -> float:
    sizes = sorted(b.size for b in blocks if b.kind is Kind.PARAGRAPH and b.size)
    return sizes[len(sizes) // 2] if sizes else 0.0


def _starts_page(blocks: list[Block], i: int) -> bool:
    """A heading opens a chapter if it is the first thing on its page or follows another such heading."""
    j = i
    while j >= 0 and blocks[j].kind is Kind.HEADING and blocks[j].page == blocks[i].page:
        if blocks[j].first_on_page:
            return True
        j -= 1
    return False


def build_chapters(blocks: list[Block]) -> list[Chapter]:
    """Decide which headings start chapters, then group blocks into chapters.

    Books (novels in particular) start every chapter on a new page, and headings in the middle of a page
    are often false positives such as a sign or a letterhead quoted in the text. So when most headings
    open a page, only those become chapters and the others stay inline. Documents whose sections run on
    (reports, papers) have mostly mid-page headings; then every top-level heading starts a chapter.
    """
    # Part titles and numbered chapter breaks found by sections.py always start a chapter.
    found = {i for i, b in enumerate(blocks) if b.kind is Kind.HEADING and b.marker in ("part", "chapter")}
    headings = [i for i, b in enumerate(blocks) if b.kind is Kind.HEADING and i not in found]
    page_starts = {i for i in headings if _starts_page(blocks, i)}
    book_mode = bool(headings) and len(page_starts) >= 0.6 * len(headings)
    if book_mode:
        # A chapter title is printed larger than the text; where sizes are known (OCR'd pages), a body-size
        # line that the layout model calls a heading does not start a chapter.
        body = median_body_size(blocks)
        page_starts = {i for i in page_starts if not body or not blocks[i].size or blocks[i].size >= MIN_TITLE_SCALE * body}
        top_level = min((blocks[i].level for i in page_starts), default=1)
        splits = {i for i in page_starts if blocks[i].level == top_level}
        for i in headings:
            if i not in page_starts:
                blocks[i].level = max(blocks[i].level, top_level + 2)  # rendered as an inline sub-heading
    else:
        top_level = min((blocks[i].level for i in headings), default=1)
        splits = {i for i in headings if blocks[i].level == top_level}
    splits |= found
    # Consecutive headings on one page ("Chapter One" / "The Beginning") belong to the same chapter opening.
    splits = {i for i in splits if not (i - 1 in splits and blocks[i - 1].page == blocks[i].page)}
    log.info(
        "Headings: %d, chapter starts: %d (%s mode, %d numbered/part)",
        len(headings) + len(found), len(splits), "book" if book_mode else "document", len(found),
    )

    chapters: list[Chapter] = []
    current: list[Block] = []

    def flush() -> None:
        if current:
            chapters.append(_make_chapter(list(current), front_matter=not splits_seen))
            current.clear()

    splits_seen = False
    for i, block in enumerate(blocks):
        if i in splits:
            flush()
            splits_seen = True
        elif not splits_seen and current and block.first_on_page:
            # Front matter (title page, copyright, dedication...) keeps one page per file.
            flush()
        elif len(current) >= MAX_BLOCKS_PER_FILE and block.kind is Kind.PARAGRAPH:
            flush()  # continuation file of a long chapter: no title, not in the TOC
        current.append(block)
    flush()
    _nest(chapters)
    _collect_notes(chapters)
    return chapters


def _nest(chapters: list[Chapter]) -> None:
    """Numbered chapters go one level below the part before them; a part's title page is listed in the
    table of contents (it has little text of its own) when chapters follow it."""
    part: Chapter | None = None
    for ch in chapters:
        first = ch.blocks[0]
        if first.marker == "part" or (ch.in_toc and first.marker != "chapter"):
            part = ch
        if first.marker == "chapter":
            ch.level = 2 if part is not None else 1
            if part is not None and part.blocks[0].marker == "part":
                part.in_toc = True


def _make_chapter(blocks: list[Block], front_matter: bool) -> Chapter:
    """Chapters that open with a heading and contain real text go in the table of contents.

    Front matter, continuation files of long chapters, and pages that hold nothing but a large display
    line (common in novels and picture books) are not listed.
    """
    title_parts = []
    for b in blocks:
        if b.kind is not Kind.HEADING or b.page != blocks[0].page:
            break
        title_parts.append(b.text)
    title = ": ".join(title_parts) or None
    body_chars = sum(len(b.text) for b in blocks if b.kind in (Kind.PARAGRAPH, Kind.LIST_ITEM, Kind.TABLE))
    text = " ".join(b.text for b in blocks)
    in_toc = (
        title is not None
        and not front_matter
        and body_chars >= MIN_CHAPTER_CHARS
        and not (len(text) < 3000 and _COPYRIGHT.search(text))
    )
    return Chapter(title=title, blocks=blocks, in_toc=in_toc)


def _collect_notes(chapters: list[Chapter]) -> None:
    """Move footnotes out of the text flow into their chapter's notes section."""
    for ch in chapters:
        ch.notes = [b for b in ch.blocks if b.kind is Kind.FOOTNOTE]
        ch.blocks = [b for b in ch.blocks if b.kind is not Kind.FOOTNOTE]
