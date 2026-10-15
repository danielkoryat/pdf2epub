"""Finds chapter breaks that carry no real heading, and part title pages.

Many novels mark chapters with nothing but a small centred numeral (א, ב, ג... / 1, 2, 3 / I, II, III)
in the middle of a page, with white space above it. OCR reads such lone characters badly or drops them
altogether, but the white space stays. So a chapter break is any of:
  * a marker: a tiny centred block (one or two characters) with white space above it,
  * a gap: unusually large white space between two paragraphs on a page (a marker OCR missed),
  * a sink: a page whose text starts much lower than usual (a chapter opening page).
Within a part (parts are separated by title pages and blank pages), breaks are only trusted when the part
has at least two markers that read as numerals; the chapters are then numbered in sequence, which also
repairs misread markers. A page holding one or a few short lines in a larger font is a part title page.
"""

from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass

from .model import Block, Kind

log = logging.getLogger(__name__)

MARKER_MAX_WIDTH = 0.05  # fraction of page width
MARKER_CENTRE_TOLERANCE = 0.08
MARKER_MIN_GAP = 0.05  # white space above a marker, fraction of page height
BREAK_MIN_GAP = 0.075  # white space between paragraphs that signals a dropped marker
SINK_MIN_TOP = 0.25  # a chapter opening page starts its text at least this low
TITLE_MIN_SCALE = 1.15  # part titles are set larger than body text

_HEB_VALUES = {"א": 1, "ב": 2, "ג": 3, "ד": 4, "ה": 5, "ו": 6, "ז": 7, "ח": 8, "ט": 9, "י": 10, "כ": 20, "ל": 30, "מ": 40}
_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def parse_numeral(text: str) -> tuple[int, str] | None:
    """(value, style) of a chapter numeral such as 'ב', 'יא', '7', 'IV', 'פרק ג', 'Chapter 3'."""
    t = re.sub(r"^(פרק|chapter|chapitre|kapitel|capítulo)\s+", "", text.strip(), flags=re.IGNORECASE)
    t = t.strip(" .:-\u2013\u2014'\"׳״")
    if t.isdigit() and len(t) <= 3:
        return int(t), "arabic"
    if t and all(ch in _HEB_VALUES for ch in t) and len(t) <= 3:
        return sum(_HEB_VALUES[ch] for ch in t), "hebrew"
    if t and all(ch in _ROMAN for ch in t.upper()) and len(t) <= 6:
        values = [_ROMAN[ch] for ch in t.upper()]
        total = sum(-v if i + 1 < len(values) and v < values[i + 1] else v for i, v in enumerate(values))
        return total, "roman"
    return None


def format_numeral(value: int, style: str) -> str:
    if style == "hebrew":
        if value == 15:
            return "טו"
        if value == 16:
            return "טז"
        letters = [(40, "מ"), (30, "ל"), (20, "כ"), (10, "י"), (9, "ט"), (8, "ח"), (7, "ז"), (6, "ו"), (5, "ה"), (4, "ד"), (3, "ג"), (2, "ב"), (1, "א")]
        out = ""
        for v, ch in letters:
            while value >= v:
                out += ch
                value -= v
        return out
    if style == "roman":
        out = ""
        for v, s in [(100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]:
            while value >= v:
                out += s
                value -= v
        return out
    return str(value)


@dataclass
class _Break:
    index: int  # block index where the new chapter starts
    marker: bool  # the block at index is the marker itself (replace it), else insert a heading before it
    numeral: tuple[int, str] | None


def _body_size(blocks: list[Block]) -> float:
    sizes = [b.size for b in blocks if b.kind is Kind.PARAGRAPH and b.size and len(b.text) > 200]
    return statistics.median(sizes) if sizes else 0.0


def _body_centre(blocks: list[Block]) -> float:
    centres = [(b.box[0] + b.box[2]) / 2 for b in blocks if b.box and b.kind is Kind.PARAGRAPH and len(b.text) > 200]
    return statistics.median(centres) if centres else 0.5


def promote_title_pages(blocks: list[Block]) -> None:
    """Make the first line of a page that holds only a few short lines in a large font a part heading."""
    body = _body_size(blocks)
    if not body:
        return
    by_page: dict[int, list[Block]] = {}
    for b in blocks:
        by_page.setdefault(b.page, []).append(b)
    title_pages = []
    for page_blocks in by_page.values():
        texts = [b for b in page_blocks if b.kind in (Kind.PARAGRAPH, Kind.HEADING)]
        if not texts or len(texts) > 3 or any(b.kind is Kind.IMAGE for b in page_blocks):
            continue
        if any(len(b.text) > 40 for b in texts) or sum(len(b.text) for b in texts) > 80:
            continue
        if max(b.size for b in texts) < TITLE_MIN_SCALE * body:
            continue
        title_pages.append(texts)
    # Parts are few. A book with many such pages uses them for display lines (a phrase alone on a page),
    # not for parts -- leave those to the normal heading logic.
    if len(title_pages) > max(4, len(by_page) // 50):
        log.info("%d pages look like title pages: treating them as display pages, not parts", len(title_pages))
        return
    for texts in title_pages:
        first = texts[0]
        if first.marker in ("known", "label"):
            continue  # already a chapter start
        first.kind, first.level, first.first_on_page, first.marker = Kind.HEADING, 1, True, "part"
        for b in texts[1:]:
            b.kind = Kind.PARAGRAPH


_LABEL = re.compile(
    r"^\s*(?P<word>פרק|chapter|chapitre|kapitel|cap[ií]tulo|capitolo|глава|hoofdstuk|rozdzia[lł])\s*(?P<num>\S{0,6})\s*$",
    re.IGNORECASE,
)


def find_labeled_chapters(blocks: list[Block]) -> None:
    """Chapters labelled "פרק ג" / "Chapter 3" / "CHAPTER III": when a book uses such labels, they are its
    chapter starts. OCR often garbles the number ("פרקר", "פרק!"), so labels are renumbered in sequence --
    provided the readable ones agree with that sequence."""
    found: list[tuple[Block, re.Match[str], tuple[int, str] | None]] = []
    for b in blocks:
        if b.kind not in (Kind.HEADING, Kind.PARAGRAPH) or len(b.text) > 16 or b.marker in ("folio", "known"):
            continue
        m = _LABEL.match(b.text)
        if m:
            found.append((b, m, parse_numeral(m.group("num")) if m.group("num") else None))
    parsed = [(n, p) for n, (_b, _m, p) in enumerate(found) if p]
    if len(found) < 3 or len(parsed) < 2:
        return
    offset = max(parsed[0][1][0] - parsed[0][0] - 1, 0)
    agree = sum(1 for n, p in parsed if p[0] == n + 1 + offset)
    if agree < 0.6 * len(parsed):
        log.info("Chapter labels found but their numbers don't follow a sequence (%d of %d)", agree, len(parsed))
        return
    style = statistics.mode(p[1] for _n, p in parsed)
    word = statistics.mode(m.group("word") for _b, m, _p in found)
    for n, (b, _m, _p) in enumerate(found):
        b.kind, b.level, b.marker, b.first_on_page = Kind.HEADING, 1, "label", True
        b.text = f"{word} {format_numeral(n + 1 + offset, style)}"
    log.info("Chapter labels: %d (%s %s ... %s)", len(found), word, format_numeral(1 + offset, style), format_numeral(len(found) + offset, style))


def find_numbered_chapters(blocks: list[Block]) -> list[Block]:
    """Insert or repair level-2 headings for numbered chapter breaks; returns the new block list."""
    centre = _body_centre(blocks)
    candidates: list[_Break] = []
    previous: Block | None = None
    for i, b in enumerate(blocks):
        if b.kind not in (Kind.PARAGRAPH, Kind.HEADING) or b.box is None or b.marker in ("part", "known", "label"):
            previous = b if b.kind not in (Kind.FOOTNOTE,) else previous
            continue
        same_page = previous is not None and previous.page == b.page and previous.box is not None
        gap = b.box[1] - previous.box[3] if same_page else None
        width = b.box[2] - b.box[0]
        short = len(b.text.replace(" ", "")) <= 3
        centred = abs((b.box[0] + b.box[2]) / 2 - centre) <= MARKER_CENTRE_TOLERANCE
        if short and width <= MARKER_MAX_WIDTH and centred and (gap is None and b.box[1] >= 0.2 or (gap or 0) >= MARKER_MIN_GAP):
            candidates.append(_Break(i, True, parse_numeral(b.text)))
        elif not short and gap is not None and gap >= BREAK_MIN_GAP:
            candidates.append(_Break(i, False, None))
        elif not short and gap is None and b.first_on_page and b.box[1] >= SINK_MIN_TOP and len(b.text) >= 60:
            candidates.append(_Break(i, False, None))
        previous = b

    if not candidates:
        return blocks
    # Split candidates into parts: a part title or a blank page (a page number with no blocks) between them.
    pages_with_blocks = {b.page for b in blocks}
    parts: list[list[_Break]] = [[]]
    for brk in candidates:
        if parts[-1]:
            last = parts[-1][-1]
            between = blocks[last.index + 1 : brk.index]
            blank_page = any(p not in pages_with_blocks for p in range(blocks[last.index].page + 1, blocks[brk.index].page))
            if blank_page or any(b.marker == "part" for b in between) or (brk.numeral and brk.numeral[0] == 1):
                parts.append([])
        parts[-1].append(brk)

    numbered: dict[int, tuple[_Break, str]] = {}
    for part in parts:
        numerals = [b.numeral for b in part if b.numeral]
        if len(numerals) < 2:
            continue
        style = statistics.mode(s for _v, s in numerals)
        # Breaks before the part's first chapter are impossible; if the first readable numeral is ahead of
        # its position, the first chapter's marker was missed (and its break too), so start counting later.
        first_numbered = next(i for i, b in enumerate(part) if b.numeral)
        offset = max(part[first_numbered].numeral[0] - first_numbered - 1, 0)
        agree = sum(1 for n, b in enumerate(part) if b.numeral and b.numeral[0] == n + 1 + offset)
        if agree < 0.7 * len(numerals):
            log.info("Chapter numbering inconsistent (%d of %d markers agree); keeping only readable markers", agree, len(numerals))
            for b in part:
                if b.numeral:
                    numbered[b.index] = (b, format_numeral(b.numeral[0], b.numeral[1]))
            continue
        for n, b in enumerate(part):
            numbered[b.index] = (b, format_numeral(n + 1 + offset, style))

    if not numbered:
        return blocks
    log.info("Numbered chapters found: %d", len(numbered))
    out: list[Block] = []
    for i, b in enumerate(blocks):
        if i in numbered:
            brk, label = numbered[i]
            if brk.marker:
                b.kind, b.text, b.level, b.marker, b.first_on_page = Kind.HEADING, label, 2, "chapter", True
            else:
                out.append(Block(Kind.HEADING, text=label, page=b.page, top=b.top, level=2, marker="chapter", first_on_page=True, box=b.box))
                b.first_on_page = False
        out.append(b)
    return out
