"""The text of a book as it should be read aloud: chapters of paragraphs, from an EPUB.

An audiobook is made from the finished EPUB (one converted from a PDF here, or one the user uploaded), so a
single reader serves both. What a listener should not hear is left out: the navigation document and contents
pages, footnotes and the reference marks that point to them, page-break markers, tables and pictures, and the
front matter before the first chapter the contents list (copyright pages, the contents itself). Chapters follow
the book's contents: an entry may point into the middle of a file, so a file can hold several chapters, and a
chapter can run over several files.

Each paragraph carries the language it should be read in, so an English quotation in a Hebrew book is read in
English. The script is saved as JSON (script.json) and handed to the voice worker; nothing in it is executable.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .cleanup import SCENE_BREAK
from .enrich import _FRONT_MATTER, NS, OPS, XHTML, EpubPackage, _body_start, _flatten
from .lang import script_counts
from .model import NOTE_CLOSE, NOTE_OPEN

FORMAT = 1
LANGUAGES = ("he", "en")  # languages the voices speak

# Elements whose text is never read: notes, navigation, pictures and tables, scripts.
_SKIP_TAGS = {"aside", "nav", "table", "figure", "img", "svg", "script", "style", "head", "audio", "video", "object", "math", "rt", "rp"}
_SKIP_TYPES = {"footnote", "footnotes", "endnote", "endnotes", "rearnote", "rearnotes", "note", "notes", "noteref", "pagebreak", "toc", "landmarks", "page-list"}
_SKIP_CLASSES = {"noteref", "footnote", "footnotes", "notes", "backlink", "pagebreak", "page-number", "pagenum"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCKS = _HEADINGS | {"p", "div", "section", "article", "blockquote", "ul", "ol", "li", "dl", "dt", "dd", "pre", "header", "footer", "main", "figcaption", "hr", "table", "figure", "aside", "nav"}
_LEAF_BLOCKS = _HEADINGS | {"p", "li", "dt", "dd", "pre", "blockquote", "div", "figcaption"}
_NOTE_MARK = re.compile(r"^\s*[\[(]?(\d{1,3}|[*\u2020\u2021\u00a7]{1,3}|[a-z])[\])]?\s*$")
_INVISIBLE = re.compile("[\u00ad\u200b\u200c\u200d\u2060\ufeff]")
_NOTE_REF = re.compile(re.escape(NOTE_OPEN) + ".*?" + re.escape(NOTE_CLOSE))


@dataclass
class Paragraph:
    text: str
    lang: str
    kind: str = "p"  # p | heading | break (a scene break: a pause, no text)


@dataclass
class Chapter:
    title: str
    paragraphs: list[Paragraph] = field(default_factory=list)


@dataclass
class Script:
    title: str
    authors: list[str]
    language: str
    chapters: list[Chapter]

    @property
    def characters(self) -> int:
        return sum(len(p.text) for ch in self.chapters for p in ch.paragraphs)


def save_script(script: Script, path: Path) -> None:
    data = {"format": FORMAT, **asdict(script)}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_script(path: Path) -> Script:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != FORMAT:
        raise ValueError("unknown script format")
    return Script(
        title=str(data["title"]),
        authors=[str(a) for a in data["authors"]],
        language=str(data["language"]),
        chapters=[Chapter(str(c["title"]), [Paragraph(str(p["text"]), str(p["lang"]), str(p["kind"])) for p in c["paragraphs"]])
                  for c in data["chapters"]],
    )


def language_of(text: str, default: str) -> str:
    """The voice language for a piece of text: by its letters, else the book's."""
    counts = script_counts(text)
    hebrew, latin = counts.get("HEBREW", 0), counts.get("LATIN", 0)
    if hebrew > latin:
        return "he"
    if latin > hebrew:
        return "en"
    return default


def clean(text: str) -> str:
    text = _NOTE_REF.sub("", _INVISIBLE.sub("", text))
    return " ".join(text.split())


def narration_script(data: bytes, title: str = "", authors: list[str] | None = None, language: str = "") -> Script:
    """The narration of an EPUB. Title, authors and language default to the book's own metadata."""
    pkg = EpubPackage(data)
    declared = [" ".join((el.text or "").split()) for el in pkg.dc("language")]
    language = (language or next(iter(declared), "")).split("-")[0].lower()
    title = title or next((" ".join((el.text or "").split()) for el in pkg.dc("title")), "")
    if authors is None:
        authors = [" ".join((el.text or "").split()) for el in pkg.dc("creator") if (el.text or "").strip()]
    reader = _Reader(pkg, language if language in LANGUAGES else "")
    chapters = reader.read()
    if not reader.language:  # no usable language tag: go by the text
        counts = script_counts(" ".join(p.text for ch in chapters for p in ch.paragraphs[:50]))
        reader.language = "he" if counts.get("HEBREW", 0) > counts.get("LATIN", 0) else "en"
        for ch in chapters:
            for p in ch.paragraphs:
                if not p.lang:
                    p.lang = reader.language
    return Script(title=title, authors=authors, language=reader.language, chapters=chapters)


class _Reader:
    def __init__(self, pkg: EpubPackage, language: str):
        self.pkg = pkg
        self.language = language
        self.chapters: list[Chapter] = []
        self.fresh = False  # a chapter just started and its title heading has not been passed yet

    def read(self) -> list[Chapter]:
        pkg = self.pkg
        toc = pkg.toc()
        docs = pkg.content_docs()
        nav = pkg.nav_item()
        skip_docs = {pkg.item_path(nav)} if nav is not None else set()
        start = _body_start(pkg, toc) if toc else (docs[0] if docs else None)
        if start not in docs:
            return []
        # Where chapters start: document -> {fragment id or "" (the top of the file): title}
        starts: dict[str, dict[str, str]] = {}
        for entry in _flatten(toc):
            if entry.path in docs and not _FRONT_MATTER.fullmatch(entry.title.strip()):
                starts.setdefault(entry.path, {}).setdefault(entry.fragment, entry.title)
        for path in docs[docs.index(start):]:
            if path in skip_docs or path not in pkg.files:
                continue
            root = pkg.parse_xhtml(path)
            if root is None:
                continue
            body = root.find(".//x:body", NS)
            if body is None or _is_contents_page(body):
                continue
            marks = starts.get(path, {})
            if "" in marks:
                self.start_chapter(marks[""])
            elif not self.chapters:
                self.start_chapter("")
            self.walk(body, {k: v for k, v in marks.items() if k})
        # A chapter with only a title (a part's title page) stays: the title is read.
        return [ch for ch in self.chapters if ch.title or any(p.kind != "break" for p in ch.paragraphs)]

    def start_chapter(self, title: str) -> None:
        if self.chapters and not any(p.kind != "break" for p in self.chapters[-1].paragraphs) and not self.chapters[-1].title:
            self.chapters[-1].title = clean(title)  # an empty untitled chapter (e.g. a cover page) takes the title
        else:
            self.chapters.append(Chapter(clean(title)))
        self.fresh = True

    def walk(self, el, marks: dict[str, str]) -> None:
        if _skipped(el):
            return
        if el.get("id") in marks:
            self.start_chapter(marks[el.get("id")])
        tag = _local(el.tag)
        if tag in _LEAF_BLOCKS and not any(_local(d.tag) in _BLOCKS for d in el.iterdescendants() if isinstance(d.tag, str)):
            for d in el.iterdescendants():  # a chapter anchor inside the paragraph (<a id="ch3"/>)
                if isinstance(d.tag, str) and d.get("id") in marks:
                    self.start_chapter(marks[d.get("id")])
            self.add(_text_of(el), heading=tag in _HEADINGS)
            return
        if el.text and el.text.strip():
            self.add(el.text)
        for child in el:
            if isinstance(child.tag, str):
                self.walk(child, marks)
            if child.tail and child.tail.strip():
                self.add(child.tail)

    def add(self, text: str, heading: bool = False) -> None:
        text = clean(text)
        if not text:
            return
        if not self.chapters:
            self.start_chapter("")
        chapter = self.chapters[-1]
        if SCENE_BREAK.match(text) or not any(ch.isalnum() for ch in text):  # "* * *", a row of ornaments
            if chapter.paragraphs and chapter.paragraphs[-1].kind != "break":
                chapter.paragraphs.append(Paragraph("", "", "break"))
            return
        if self.fresh:
            if heading and not chapter.title:
                chapter.title = text
                return
            if len(text) < 120 and _same(text, chapter.title):
                return  # the chapter's own title (as a heading or a styled paragraph): it is read from the title
        if not heading:
            self.fresh = False
        lang = language_of(text, self.language)
        chapter.paragraphs.append(Paragraph(text, lang, "heading" if heading else "p"))


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _skipped(el) -> bool:
    if not isinstance(el.tag, str):
        return True  # comments, processing instructions
    tag = _local(el.tag)
    if tag in _SKIP_TAGS:
        return True
    types = set((el.get(f"{{{OPS}}}type") or "").lower().split()) | set((el.get("role") or "").lower().replace("doc-", "").split())
    if types & _SKIP_TYPES:
        return True
    if set((el.get("class") or "").lower().split()) & _SKIP_CLASSES:
        return True
    if tag in ("a", "sup") and _NOTE_MARK.match("".join(el.itertext())) and (tag == "sup" or (el.get("href") or "").find("#") >= 0):
        return True  # a footnote mark: "1", "[2]", "*"
    return False


def _text_of(el) -> str:
    parts: list[str] = []

    def collect(node) -> None:
        if node.text:
            parts.append(node.text)
        for child in node:
            if isinstance(child.tag, str) and not _skipped(child):
                if _local(child.tag) == "br":
                    parts.append(" ")
                collect(child)
            if child.tail:
                parts.append(child.tail)

    collect(el)
    return "".join(parts)


def _is_contents_page(body) -> bool:
    """A contents page: mostly links (to chapters), or marked as such."""
    for el in body.iter():
        if isinstance(el.tag, str) and "toc" in (el.get(f"{{{OPS}}}type") or "").lower().split():
            return True
    text = clean(" ".join(body.itertext()))
    links = [a for a in body.iter(f"{{{XHTML}}}a") if a.get("href")]
    linked = sum(len(clean(" ".join(a.itertext()))) for a in links)
    return len(links) >= 3 and linked >= 0.6 * max(len(text), 1)


def _same(heading: str, title: str) -> bool:
    """A heading that repeats (part of) the chapter title: "CHAPTER ONE" under "Chapter One: The Shell"."""
    norm = lambda s: re.sub(r"\W+", "", s).lower()  # noqa: E731
    h, t = norm(heading), norm(title)
    return bool(h and t) and (h in t or t in h)
