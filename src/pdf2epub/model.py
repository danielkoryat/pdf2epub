"""The neutral Book model that sits between extraction and EPUB writing."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# Private-use characters that mark a footnote reference inside block text: NOTE_OPEN + note id + NOTE_CLOSE.
# They survive all text clean-up and are turned into <a epub:type="noteref"> links by the writer.
NOTE_OPEN = ""
NOTE_CLOSE = ""


class Kind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    FOOTNOTE = "footnote"
    IMAGE = "image"
    TABLE = "table"
    CAPTION = "caption"
    CODE = "code"


@dataclass
class Block:
    kind: Kind
    text: str = ""
    page: int = 0  # 1-based PDF page the block starts on
    top: float = 0.0  # distance from the top of the page, as a fraction of page height
    # (left, top, right, bottom) as fractions of the page size, when known
    box: tuple[float, float, float, float] | None = None
    first_on_page: bool = False  # first body block (not furniture) on its page
    level: int = 1  # heading level
    size: float = 0.0  # text height in points when known (OCR'd pages), else 0
    # list item / footnote marker as printed; for headings found by sections.py: "part" or "chapter"
    marker: str = ""
    enumerated: bool = False  # ordered list item
    image: bytes | None = None
    image_ext: str = ""
    width: int = 0
    height: int = 0
    html: str = ""  # tables
    note_id: str = ""  # footnotes: id that NOTE_OPEN+id+NOTE_CLOSE references point to
    linked: bool = False  # footnotes: a reference to it was found in the body text
    pages: list[int] = field(default_factory=list)  # every page the block spans (after merging)

    def __post_init__(self) -> None:
        if not self.pages:
            self.pages = [self.page]


@dataclass
class Chapter:
    title: str | None
    blocks: list[Block]
    in_toc: bool = True
    notes: list[Block] = field(default_factory=list)
    level: int = 1  # table-of-contents depth: 1 = part or chapter, 2 = chapter inside a part


@dataclass
class Book:
    title: str
    authors: list[str]
    language: str  # BCP 47, e.g. "he", "en"
    direction: str  # "rtl" | "ltr"
    chapters: list[Chapter]
    cover: bytes | None = None
    cover_ext: str = "jpg"
