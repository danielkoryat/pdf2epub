"""Reviewing a Draft before it is written: chapter summaries/previews and applying the user's edits."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

from .lang import direction_for_language
from .model import NOTE_CLOSE, NOTE_OPEN, Block, Book, Chapter, Kind

_NOTE_REF = re.compile(f"{NOTE_OPEN}.*?{NOTE_CLOSE}")


def plain(text: str) -> str:
    """Block text without footnote placeholders."""
    return _NOTE_REF.sub("*", text)


def _pages(ch: Chapter) -> tuple[int, int]:
    pages = [p for b in ch.blocks for p in b.pages] or [0]
    return min(pages), max(pages)


def chapter_summaries(book: Book) -> list[dict]:
    out = []
    for i, ch in enumerate(book.chapters):
        first, last = _pages(ch)
        text_blocks = [b for b in ch.blocks if b.kind in (Kind.PARAGRAPH, Kind.LIST_ITEM)]
        snippet = plain(text_blocks[0].text)[:180] if text_blocks else ""
        out.append(
            {
                "index": i,
                "title": ch.title or "",
                "in_toc": ch.in_toc,
                "level": ch.level,
                "first_page": first,
                "last_page": last,
                "words": sum(len(b.text.split()) for b in ch.blocks),
                "images": sum(b.kind is Kind.IMAGE for b in ch.blocks),
                "notes": len(ch.notes),
                "snippet": snippet,
            }
        )
    return out


def chapter_preview(book: Book, index: int, limit: int = 60) -> list[dict]:
    ch = book.chapters[index]
    items = []
    for n, b in enumerate(ch.blocks[:limit]):
        item = {"n": n, "kind": str(b.kind), "text": plain(b.text), "level": b.level, "page": b.page}
        if b.kind is Kind.TABLE:
            item["text"] = plain(b.text)[:300]
        items.append(item)
    items += [{"n": -1, "kind": "footnote", "text": plain(note.text), "level": 0, "page": note.page} for note in ch.notes]
    return items


@dataclass
class ChapterEdit:
    index: int
    title: str | None = None  # new title; None keeps the current one
    in_toc: bool | None = None
    merge: bool = False  # append this chapter to the previous one (its heading becomes an inline sub-heading)
    drop: bool = False  # remove the chapter entirely


@dataclass
class Edits:
    title: str | None = None
    authors: list[str] | None = None
    language: str | None = None
    cover: bool = True
    chapters: list[ChapterEdit] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict) -> "Edits":
        chapters = [ChapterEdit(**{k: v for k, v in c.items() if k in ChapterEdit.__dataclass_fields__}) for c in data.get("chapters", [])]
        authors = data.get("authors")
        if isinstance(authors, str):
            authors = [a.strip() for a in re.split(r"[,;\n]", authors) if a.strip()]
        return cls(
            title=(data.get("title") or "").strip() or None,
            authors=authors,
            language=(data.get("language") or "").strip() or None,
            cover=bool(data.get("cover", True)),
            chapters=chapters,
        )


def _retitle(ch: Chapter, title: str) -> None:
    """Replace the chapter's opening heading(s) with a single heading carrying the new title."""
    opening = 0
    while opening < len(ch.blocks) and ch.blocks[opening].kind is Kind.HEADING and ch.blocks[opening].page == ch.blocks[0].page:
        opening += 1
    page = ch.blocks[0].page if ch.blocks else 0
    level = ch.blocks[0].level if opening else 1
    ch.blocks[:opening] = [Block(Kind.HEADING, text=title, page=page, level=level, first_on_page=True)]
    ch.title = title


def apply_edits(book: Book, edits: Edits) -> Book:
    """Return a copy of `book` with the edits applied; the original stays untouched for later re-edits."""
    book = copy.deepcopy(book)
    if edits.title:
        book.title = edits.title
    if edits.authors is not None:
        book.authors = edits.authors
    if edits.language:
        book.language = edits.language
        book.direction = direction_for_language(edits.language)
    if not edits.cover:
        book.cover = None

    by_index = {e.index: e for e in edits.chapters}
    kept: list[Chapter] = []
    for i, ch in enumerate(book.chapters):
        e = by_index.get(i, ChapterEdit(index=i))
        if e.drop:
            continue
        if e.title is not None and e.title.strip() and e.title.strip() != (ch.title or ""):
            _retitle(ch, e.title.strip())
        if e.in_toc is not None:
            ch.in_toc = e.in_toc and bool(ch.title)
        if e.merge and kept:
            previous = kept[-1]
            for b in ch.blocks:
                if b.kind is Kind.HEADING:
                    b.level = max(b.level, 2)  # rendered as an inline sub-heading, not a chapter title
            previous.blocks.extend(ch.blocks)
            previous.notes.extend(ch.notes)
            continue
        kept.append(ch)
    book.chapters = kept
    return book
