"""Turns the raw, page-by-page block list into continuous book text."""

from __future__ import annotations

import logging
import re
from collections import Counter

from .lang import base_direction
from .model import NOTE_CLOSE, NOTE_OPEN, Block, Kind
from .sections import find_numbered_chapters, promote_title_pages

log = logging.getLogger(__name__)

# A paragraph that ends with one of these is complete; otherwise it continues on the next page.
_TERMINAL = tuple(".!?…:;\"”’'»״)]׃。！？")
_PAGE_NUMBER = re.compile(r"^[\s\-\u2013\u2014]*(\d{1,4}|[ivxlcdm]{1,6})[\s\-\u2013\u2014]*$", re.IGNORECASE)
_LATIN_HYPHEN_BREAK = re.compile(r"([a-zà-öø-ÿ])[-­] +([a-zà-öø-ÿ])")
DECORATIVE_SCALE = 5.0
SCENE_BREAK = re.compile(r"^[\s*·•~#⁂◆◇❖✦⁎=_\-\u2013\u2014]{1,20}$")
_FOOTNOTE_MARKER = re.compile(r"^\s*(\*{1,4}|[†‡§¶]{1,3}|\d{1,3}|[¹²³⁴⁵⁶⁷⁸⁹⁰]{1,3})[\s.)\]]*")


def clean(blocks: list[Block], page_count: int) -> list[Block]:
    blocks = [_normalise(b) for b in blocks]
    blocks = [b for b in blocks if b.kind in (Kind.IMAGE, Kind.TABLE) or _has_content(b)]
    blocks = _drop_decorative(blocks)
    blocks = _drop_page_numbers(blocks)
    blocks = _drop_repeated_lines(blocks, page_count)
    # While every block still belongs to one page: find part title pages and numbered chapter breaks (they
    # rely on white space between blocks of a page), and link footnotes so a marker is only matched against
    # text printed on the same page as its note.
    promote_title_pages(blocks)
    blocks = find_numbered_chapters(blocks)
    _link_footnotes(blocks)
    return _merge_across_pages(blocks)


def _normalise(block: Block) -> Block:
    text = re.sub(r"[ \t\r\n ]+", " ", block.text).strip()
    if base_direction(text) == "ltr":
        text = _LATIN_HYPHEN_BREAK.sub(r"\1\2", text)
    block.text = text
    return block


def _has_content(block: Block) -> bool:
    """Filter OCR noise.

    Text with no letters or digits (specks, stray dashes) is noise unless it is a scene break. Short text
    that is mostly symbols is what OCR makes of decorative lettering or drawings, e.g. `טו\\י\\,/ = אוהה`.
    """
    chars = [ch for ch in block.text if not ch.isspace()]
    alnum = sum(ch.isalnum() for ch in chars)
    if not alnum:
        return block.kind is Kind.PARAGRAPH and bool(SCENE_BREAK.match(block.text)) and len(chars) >= 3
    return len(block.text) >= 80 or alnum / len(chars) >= 0.6


def _drop_decorative(blocks: list[Block]) -> list[Block]:
    """Drop short OCR text set many times larger than the body text: decorative lettering on covers and
    title pages, which OCR turns into junk (the page image itself is kept as the cover)."""
    sizes = sorted(b.size for b in blocks if b.kind is Kind.PARAGRAPH and b.size)
    if not sizes:
        return blocks
    limit = DECORATIVE_SCALE * sizes[len(sizes) // 2]
    return [b for b in blocks if not (b.size > limit and len(b.text) < 40)]


def _drop_page_numbers(blocks: list[Block]) -> list[Block]:
    """Remove printed page numbers that the layout model did not tag as page header/footer."""
    return [
        b
        for b in blocks
        if not (b.kind is Kind.PARAGRAPH and _PAGE_NUMBER.match(b.text) and (b.top < 0.1 or b.top > 0.85))
    ]


def _drop_repeated_lines(blocks: list[Block], page_count: int) -> list[Block]:
    """Remove running headers and watermarks: short text that repeats on many pages.

    Text without letters (scene breaks such as '* * *') is kept, since it repeats on purpose.
    """
    if page_count < 6:
        return blocks
    counts = Counter(b.text for b in blocks if b.kind is Kind.PARAGRAPH and len(b.text) < 120)
    threshold = max(4, int(0.2 * page_count))
    repeated = {t for t, n in counts.items() if n >= threshold and any(ch.isalpha() for ch in t)}
    if repeated:
        log.info("Dropping repeated lines: %s", sorted(repeated)[:10])
    return [b for b in blocks if not (b.kind is Kind.PARAGRAPH and b.text in repeated)]


def _ends_sentence(text: str) -> bool:
    return text.rstrip().endswith(_TERMINAL)


def _merge_across_pages(blocks: list[Block]) -> list[Block]:
    """Join a paragraph that was cut by a page break with its continuation on the next page.

    Footnotes and images printed between the two halves stay where they are; they are moved out of the
    text flow later anyway (footnotes) or simply follow the joined paragraph (images).
    """
    out: list[Block] = []
    open_para: Block | None = None  # last paragraph, if nothing but footnotes/images came after it
    for block in blocks:
        if (
            block.kind is Kind.PARAGRAPH
            and open_para is not None
            and block.first_on_page
            and block.page > open_para.pages[-1]
            and not _ends_sentence(open_para.text)
        ):
            if open_para.text.endswith(("-", "­")) and base_direction(open_para.text) == "ltr" and block.text[:1].islower():
                open_para.text = open_para.text.rstrip("-­") + block.text
            else:
                open_para.text = f"{open_para.text} {block.text}"
            open_para.pages.append(block.page)
            continue
        out.append(block)
        if block.kind is Kind.PARAGRAPH:
            open_para = block
        elif block.kind not in (Kind.FOOTNOTE, Kind.IMAGE, Kind.CAPTION):
            open_para = None
    return out


def _marker_pattern(marker: str) -> re.Pattern[str]:
    if marker.isdigit():
        # A digit glued to the end of a word or punctuation: "word12" / "word.12" -- not a free-standing number.
        return re.compile(rf"(?<=[^\s\d{NOTE_CLOSE}]){marker}(?!\d)")
    first = re.escape(marker[0])
    return re.compile(rf"(?<!{first}){re.escape(marker)}(?!{first})")


def _link_footnotes(blocks: list[Block]) -> None:
    """Give every footnote an id and replace its marker in the body text with a reference placeholder."""
    counter = 0
    for i, note in enumerate(blocks):
        if note.kind is not Kind.FOOTNOTE:
            continue
        counter += 1
        note.note_id = f"note-{counter}"
        match = _FOOTNOTE_MARKER.match(note.text)
        if not match:
            continue
        marker = match.group(1)
        pattern = _marker_pattern(marker)
        # Search backwards through text on the same page for the marker that points to this note.
        for body in reversed(blocks[:i]):
            if body.page < note.page:
                break
            if body.kind not in (Kind.PARAGRAPH, Kind.LIST_ITEM, Kind.HEADING, Kind.CAPTION):
                continue
            found = None
            for m in pattern.finditer(body.text):
                found = m  # the last occurrence is the closest to the note
            if found:
                body.text = f"{body.text[:found.start()]}{NOTE_OPEN}{note.note_id}{NOTE_CLOSE}{body.text[found.end():]}"
                note.marker = marker
                note.text = note.text[match.end():].strip()
                note.linked = True
                break
