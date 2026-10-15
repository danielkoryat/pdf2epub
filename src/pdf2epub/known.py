"""Chapter starts the book itself declares: PDF bookmarks and a printed contents page.

Layout heuristics have to guess which headings start chapters. Many books say so explicitly, which is far
more reliable:
  * born-digital PDFs often carry an outline (bookmarks) with each chapter's title and page;
  * printed books have a contents page: "title ... page number" lines. Printed page numbers are mapped to PDF
    pages through the page numbers printed at the top or bottom of the pages.
Each declared chapter is matched to the heading (or short line) with that title on its page; when a source
is trusted, the chapters it lists replace the guessed ones (see structure.build_chapters).
"""

from __future__ import annotations

import logging
import re
import statistics
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from .model import Block, Kind

log = logging.getLogger(__name__)

PAGE_NUMBER = re.compile(r"^[\s\-\u2013\u2014\[\]()]*(\d{1,4})[\s\-\u2013\u2014\[\]()]*$")
_ENTRY = re.compile(r"(?P<title>[^\d|]{2,}?)[\s.·…_\-\u2013\u2014]*(?P<num>\d{1,4})(?!\d)")
MATCH_RATIO = 0.8
MIN_TRUSTED_SHARE = 0.6  # a contents page is trusted when this share of its entries is found in the book


@dataclass
class Declared:
    title: str
    page: int  # 1-based PDF page the chapter starts on (an estimate for contents-page entries)
    level: int = 1
    exact_page: bool = True  # bookmarks point at the page; contents pages need a search around it


def normalise(text: str) -> str:
    """Lower case, no diacritics (Hebrew points too), no punctuation, single spaces."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return " ".join(text.split())


def similar(a: str, b: str) -> bool:
    a, b = normalise(a), normalise(b)
    if not a or not b:
        return False
    if a == b or (len(a) >= 4 and (b.startswith(a) or a.startswith(b))):
        return True
    return SequenceMatcher(None, a, b).ratio() >= MATCH_RATIO


def printed_page_numbers(blocks: list[Block]) -> dict[int, int]:
    """PDF page -> page number printed on it (from number-only lines at the top or bottom of the page)."""
    found: dict[int, int] = {}
    for b in blocks:
        if b.kind in (Kind.PARAGRAPH, Kind.HEADING) and (b.marker == "folio" or b.top < 0.1 or b.top > 0.85):
            m = PAGE_NUMBER.match(b.text)
            if m:
                found.setdefault(b.page, int(m.group(1)))
    return found


def page_offset(printed: dict[int, int]) -> int | None:
    """The usual difference between PDF page and printed page number, when it is consistent."""
    diffs = [pdf - num for pdf, num in printed.items()]
    if len(diffs) < 3:
        return None
    common = statistics.mode(diffs)
    return common if diffs.count(common) >= 0.5 * len(diffs) else None


def outline_entries(toc: list[list], first_page: int = 1, page_count: int | None = None) -> list[Declared]:
    """PDF bookmarks ([level, title, page] from PyMuPDF) as declared chapters, for pages first_page...;
    page numbers are made relative to first_page."""
    out = []
    for item in toc:
        level, title, page = item[0], (item[1] or "").strip(), item[2]
        if not title or page < first_page:
            continue
        rel = page - first_page + 1
        if page_count is not None and rel > page_count:
            continue
        out.append(Declared(title=title, page=rel, level=min(level, 2)))
    return out


def _parse_entries(text: str) -> list[tuple[str, int]]:
    entries = []
    for m in _ENTRY.finditer(text):
        title = " ".join(m.group("title").split()).strip(" .·…-:")
        if len(normalise(title)) >= 2:
            entries.append((title, int(m.group("num"))))
    return entries


def contents_page_candidates(blocks: list[Block], page_count: int) -> list[tuple[int, list[tuple[str, int]]]]:
    """Pages near the start or end of the book that look like a printed contents page:
    (PDF page, [(title, printed page)]), with at least three entries whose page numbers mostly rise."""
    pages = sorted({b.page for b in blocks})
    near_ends = [p for p in pages if p <= max(15, page_count * 0.15) or p > page_count - max(10, page_count * 0.1)]
    max_printed = int(page_count * 1.2) + 20  # years and other large numbers are not page numbers
    found = []
    for page in near_ends:
        text = " | ".join(b.text for b in blocks if b.page == page and b.kind in (Kind.PARAGRAPH, Kind.HEADING, Kind.LIST_ITEM))
        entries = [(t, n) for t, n in _parse_entries(text) if 0 < n <= max_printed]
        if len(entries) < 3:
            continue
        numbers = [n for _t, n in entries]
        rising = sum(1 for a, b in zip(numbers, numbers[1:]) if b > a)
        if rising >= 0.6 * (len(numbers) - 1):
            found.append((page, entries))
    return found


def _candidates(blocks: list[Block], title: str) -> list[int]:
    limit = 3 * len(title) + 20
    return [i for i, b in enumerate(blocks) if b.kind in (Kind.HEADING, Kind.PARAGRAPH) and len(b.text) <= limit and similar(b.text, title)]


def apply_declared(blocks: list[Block], declared: list[Declared], after_page: int = 0) -> tuple[list[Block], int]:
    """Mark the start of each declared chapter as a heading with marker "known"; returns the new block list
    and how many entries were found in the text (a missing bookmark target gets a heading inserted)."""
    blocks = list(blocks)
    matched = 0
    used: set[int] = set()
    inserts: list[tuple[int, Block]] = []
    for d in declared:
        options = [i for i in _candidates(blocks, d.title) if i not in used and blocks[i].page > after_page]
        if d.exact_page:
            on_page = [i for i in options if blocks[i].page == d.page]
        else:
            on_page = sorted((i for i in options if abs(blocks[i].page - d.page) <= 2), key=lambda i: abs(blocks[i].page - d.page))
            if not on_page:  # the page number was misread or the offset is unknown: the next match in the text
                on_page = [i for i in options if blocks[i].page >= d.page - 2] or options
        if on_page:
            i = on_page[0]
            used.add(i)
            b = blocks[i]
            b.kind, b.level, b.marker, b.first_on_page = Kind.HEADING, d.level, "known", True
            matched += 1
        elif d.exact_page:
            first = next((i for i, b in enumerate(blocks) if b.page == d.page), None)
            if first is not None:
                inserts.append((first, Block(Kind.HEADING, text=d.title, page=d.page, level=d.level, marker="known", first_on_page=True, top=blocks[first].top)))
    for i, block in sorted(inserts, key=lambda x: x[0], reverse=True):
        blocks[i].first_on_page = False
        blocks.insert(i, block)
    return blocks, matched


def use_declared_chapters(blocks: list[Block], page_count: int, outline: list[Declared] | None) -> list[Block]:
    """Apply bookmarks if the PDF has them, else a printed contents page if one is found and trusted."""
    if outline and len(outline) >= 2:
        blocks, matched = apply_declared(blocks, outline)
        log.info("Chapters from PDF bookmarks: %d (%d matched a heading in the text)", len(outline), matched)
        return blocks
    offset = page_offset(printed_page_numbers(blocks))
    best: tuple[int, list[Block], int, int] | None = None  # (matched, blocks, page, entries)
    for page, entries in contents_page_candidates(blocks, page_count):
        declared = [Declared(title=t, page=(n + offset) if offset is not None else page + 1, exact_page=False) for t, n in entries]
        trial = [Block(**{**b.__dict__, "pages": list(b.pages)}) for b in blocks]
        trial, matched = apply_declared(trial, declared, after_page=page)
        log.info("Possible contents page %d: %d of %d entries found in the text (page offset %s)", page, matched, len(entries), offset)
        if matched >= max(2, MIN_TRUSTED_SHARE * len(entries)) and (best is None or matched > best[0]):
            best = (matched, trial, page, len(entries))
    if best is None:
        return blocks
    matched, trial, page, total = best
    log.info("Chapters from the printed contents page (page %d): %d of %d entries", page, matched, total)
    # The printed contents page refers to printed page numbers; the EPUB gets its own contents page instead.
    return [b for b in trial if b.page != page]
