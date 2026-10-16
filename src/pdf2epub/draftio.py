"""Saving and loading prepared drafts as JSON.

Drafts used to be pickled. A pickle runs code when loaded, so in server mode, where the sandboxed worker writes
drafts and the web process reads them, a worker taken over by a malicious PDF could plant a pickle that runs code
in the web process, which holds the secrets. JSON cannot do that: only known fields are read, with their types.
"""

from __future__ import annotations

import base64
import dataclasses
import json
from pathlib import Path
from typing import Any

from .model import Block, Book, Chapter, Kind
from .pipeline import Draft

FORMAT = 1


def _bytes(value: bytes | None) -> str | None:
    return None if value is None else base64.b64encode(value).decode("ascii")


def _unbytes(value: Any) -> bytes | None:
    return None if value is None else base64.b64decode(str(value), validate=True)


def _block_out(b: Block) -> dict[str, Any]:
    data = dataclasses.asdict(b)
    data["kind"] = b.kind.value
    data["image"] = _bytes(b.image)
    return data


_BLOCK_TYPES = {f.name: f.type for f in dataclasses.fields(Block)}


def _block_in(data: dict[str, Any]) -> Block:
    values: dict[str, Any] = {}
    for name in _BLOCK_TYPES:
        if name not in data:
            continue
        value = data[name]
        if name == "kind":
            value = Kind(value)
        elif name == "image":
            value = _unbytes(value)
        elif name == "box":
            value = None if value is None else tuple(float(x) for x in value)[:4]
        elif name == "pages":
            value = [int(x) for x in value]
        elif name in ("text", "marker", "image_ext", "html", "note_id"):
            value = str(value)
        elif name in ("page", "level", "width", "height"):
            value = int(value)
        elif name in ("top", "size"):
            value = float(value)
        elif name in ("first_on_page", "enumerated", "linked"):
            value = bool(value)
        values[name] = value
    return Block(**values)


def _chapter_out(ch: Chapter) -> dict[str, Any]:
    return {
        "title": ch.title,
        "in_toc": ch.in_toc,
        "level": ch.level,
        "blocks": [_block_out(b) for b in ch.blocks],
        "notes": [_block_out(n) for n in ch.notes],
    }


def _chapter_in(data: dict[str, Any]) -> Chapter:
    return Chapter(
        title=None if data.get("title") is None else str(data["title"]),
        blocks=[_block_in(b) for b in data.get("blocks", [])],
        in_toc=bool(data.get("in_toc", True)),
        notes=[_block_in(n) for n in data.get("notes", [])],
        level=int(data.get("level", 1)),
    )


def draft_to_dict(draft: Draft) -> dict[str, Any]:
    book = draft.book
    return {
        "format": FORMAT,
        "pages": draft.pages,
        "scanned_pages": draft.scanned_pages,
        "ocr_lang": draft.ocr_lang,
        "ocr_fixes": draft.ocr_fixes,
        "seconds": draft.seconds,
        "warnings": list(draft.warnings),
        "book": {
            "title": book.title,
            "authors": list(book.authors),
            "language": book.language,
            "direction": book.direction,
            "cover": _bytes(book.cover),
            "cover_ext": book.cover_ext,
            "chapters": [_chapter_out(ch) for ch in book.chapters],
        },
    }


def draft_from_dict(data: dict[str, Any]) -> Draft:
    if data.get("format") != FORMAT:
        raise ValueError("unknown draft format")
    b = data["book"]
    book = Book(
        title=str(b["title"]),
        authors=[str(a) for a in b.get("authors", [])],
        language=str(b["language"]),
        direction="rtl" if b.get("direction") == "rtl" else "ltr",
        chapters=[_chapter_in(ch) for ch in b.get("chapters", [])],
        cover=_unbytes(b.get("cover")),
        cover_ext=str(b.get("cover_ext") or "jpg"),
    )
    return Draft(
        book=book,
        pages=int(data["pages"]),
        scanned_pages=int(data["scanned_pages"]),
        ocr_lang=None if data.get("ocr_lang") is None else str(data["ocr_lang"]),
        ocr_fixes=int(data.get("ocr_fixes", 0)),
        seconds=float(data.get("seconds", 0.0)),
        warnings=[str(w) for w in data.get("warnings", [])],
    )


def save_draft(draft: Draft, path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(draft_to_dict(draft), ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_draft(path: Path) -> Draft:
    return draft_from_dict(json.loads(path.read_text(encoding="utf-8")))
