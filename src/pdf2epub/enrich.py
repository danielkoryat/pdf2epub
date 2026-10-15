"""Enriching existing EPUB files.

The book is edited in place, not rebuilt: every file is copied unchanged except the few this module touches
(the package document, the navigation document, chapter start tags) and the files it adds (a contents page,
a navigation document for EPUB 2 books, a cover image). That keeps the publisher's markup and styling intact.

What can be added or fixed:
  * language: set when missing, corrected when the text is plainly in another script (a Hebrew book tagged
    "en"), or set by the user; title and authors can be changed too;
  * right-to-left books: RTL page progression, Kindle's writing mode, lang/dir on chapters that lack them;
  * a contents page in the reading order, built from the book's navigation, when the book has none;
  * navigation landmarks (contents, start of the text);
  * schema.org accessibility metadata;
  * the cover: an image marked the EPUB 2 way is marked the EPUB 3 way too, or a new cover image is set;
  * EPUB 2 books are upgraded to EPUB 3 (navigation document from the NCX, modified date, HTML5 doctype);
  * optionally, the OCR letter fixes of spell.py on the text.
"""

from __future__ import annotations

import html
import io
import logging
import posixpath
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

from .lang import direction_for_language, language_for_script, script_counts
from .writer import accessibility_metadata, contents_label

log = logging.getLogger(__name__)

OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
XHTML = "http://www.w3.org/1999/xhtml"
OPS = "http://www.idpf.org/2007/ops"
NCX = "http://www.daisy.org/z3986/2005/ncx/"
CONTAINER = "urn:oasis:names:tc:opendocument:xmlns:container"
NS = {"opf": OPF, "dc": DC, "x": XHTML, "epub": OPS, "ncx": NCX, "c": CONTAINER}

CONTENTS_ID = "pdf2epub-contents"
NAV_ID = "pdf2epub-nav"
COVER_ID = "pdf2epub-cover"

# Scripts a language is written in, to tell a wrong language tag from a merely different language.
_LANG_SCRIPTS = {
    "he": {"HEBREW"}, "yi": {"HEBREW"}, "ar": {"ARABIC"}, "fa": {"ARABIC"}, "ur": {"ARABIC"},
    "ru": {"CYRILLIC"}, "uk": {"CYRILLIC"}, "bg": {"CYRILLIC"}, "sr": {"CYRILLIC", "LATIN"}, "el": {"GREEK"},
    "zh": {"CJK"}, "ja": {"CJK", "HIRAGANA", "KATAKANA"}, "ko": {"HANGUL", "CJK"}, "hi": {"DEVANAGARI"},
    "th": {"THAI"}, "hy": {"ARMENIAN"}, "ka": {"GEORGIAN"},
}
# Navigation entries that are front matter, not the start of the text.
_FRONT_MATTER = re.compile(
    r"cover|title\s*page|copyright|dedication|contents|epigraph|half\s*title|"
    r"עטיפה|כריכה|שער|זכויות|הקדשה|תוכן|מוטו",
    re.IGNORECASE,
)
_HTML_TAG = re.compile(r"<html\b[^>]*>", re.IGNORECASE)
_DOCTYPE = re.compile(r"<!DOCTYPE[^>]*>", re.IGNORECASE | re.DOTALL)
_TABLE_SUMMARY = re.compile(r"(<table\b[^>]*?)\s+summary=(\"[^\"]*\"|'[^']*')", re.IGNORECASE)
_META_TAG = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)


def _fix_content_type_meta(text: str) -> str:
    """HTML5 only allows http-equiv content-type with the value "text/html; charset=utf-8"."""

    def fix(m: re.Match[str]) -> str:
        tag = m.group(0)
        if not re.search(r"http-equiv=[\"']content-type[\"']", tag, re.IGNORECASE):
            return tag
        return re.sub(r"(content=[\"'])[^\"']*([\"'])", r"\1text/html; charset=utf-8\2", tag, count=1, flags=re.IGNORECASE)

    return _META_TAG.sub(fix, text)


@dataclass
class TocEntry:
    title: str
    path: str  # zip path of the target document
    fragment: str = ""
    children: list["TocEntry"] = field(default_factory=list)


@dataclass
class EnrichOptions:
    title: str | None = None
    authors: list[str] | None = None
    language: str | None = None  # BCP 47; overrides the detected one
    cover_image: bytes | None = None  # a new cover (JPEG or PNG)
    fix_language: bool = True
    direction: bool = True
    contents_page: bool = True
    landmarks: bool = True
    accessibility: bool = True
    cover: bool = True  # mark an existing cover image properly
    upgrade: bool = True  # EPUB 2 -> EPUB 3
    ocr_fixes: bool = False


@dataclass
class EpubInfo:
    version: str
    title: str
    authors: list[str]
    language: str | None
    detected_language: str | None
    direction: str
    has_nav: bool
    has_ncx: bool
    has_contents_page: bool
    has_landmarks: bool
    has_cover: bool
    cover_marked: bool
    has_accessibility: bool
    rtl_ok: bool  # RTL page progression and writing mode present (or not needed)
    chapters: int
    toc_entries: int
    words: int
    suggestions: list[str] = field(default_factory=list)


class EpubPackage:
    """An EPUB opened for editing: all zip entries in memory, the package document parsed."""

    def __init__(self, data: bytes):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.names = [n for n in z.namelist() if not n.endswith("/")]
            self.files = {n: z.read(n) for n in self.names}
        container = etree.fromstring(self.files["META-INF/container.xml"])
        rootfile = container.find(".//c:rootfile", NS)
        if rootfile is None:
            raise ValueError("Not an EPUB: META-INF/container.xml names no package document")
        self.opf_path = rootfile.get("full-path")
        self.opf_dir = posixpath.dirname(self.opf_path)
        self.opf = etree.fromstring(self.files[self.opf_path])
        self.changed: set[str] = set()

    @classmethod
    def open(cls, path: Path) -> "EpubPackage":
        return cls(Path(path).read_bytes())

    # -- package document ----------------------------------------------------------------------------------

    @property
    def version(self) -> str:
        return self.opf.get("version", "2.0")

    @property
    def is_epub3(self) -> bool:
        return self.version.startswith("3")

    @property
    def metadata(self):
        return self.opf.find("opf:metadata", NS)

    @property
    def manifest(self):
        return self.opf.find("opf:manifest", NS)

    @property
    def spine(self):
        return self.opf.find("opf:spine", NS)

    def items(self) -> list:
        return self.manifest.findall("opf:item", NS)

    def item(self, item_id: str):
        return next((i for i in self.items() if i.get("id") == item_id), None)

    def item_path(self, item) -> str:
        return self.resolve(self.opf_dir, item.get("href"))

    @staticmethod
    def resolve(base_dir: str, href: str) -> str:
        from urllib.parse import unquote

        return posixpath.normpath(posixpath.join(base_dir, unquote(href.split("#")[0]))).lstrip("./") if href else ""

    def item_by_path(self, path: str):
        return next((i for i in self.items() if self.item_path(i) == path), None)

    def properties(self, item) -> set[str]:
        return set((item.get("properties") or "").split())

    def spine_items(self) -> list:
        out = []
        for ref in self.spine.findall("opf:itemref", NS):
            item = self.item(ref.get("idref"))
            if item is not None:
                out.append((ref, item))
        return out

    def content_docs(self) -> list[str]:
        return [self.item_path(i) for _ref, i in self.spine_items() if "html" in (i.get("media-type") or "")]

    def nav_item(self):
        return next((i for i in self.items() if "nav" in self.properties(i)), None)

    def ncx_item(self):
        toc_id = self.spine.get("toc")
        item = self.item(toc_id) if toc_id else None
        return item if item is not None else next((i for i in self.items() if i.get("media-type") == "application/x-dtbncx+xml"), None)

    def unique_id(self, base: str) -> str:
        ids = {i.get("id") for i in self.items()}
        candidate, n = base, 1
        while candidate in ids:
            n += 1
            candidate = f"{base}-{n}"
        return candidate

    def unique_path(self, path: str) -> str:
        stem, ext = posixpath.splitext(path)
        candidate, n = path, 1
        while candidate in self.files:
            n += 1
            candidate = f"{stem}-{n}{ext}"
        return candidate

    def add_file(self, path: str, data: bytes, media_type: str, item_id: str, properties: str = "") -> object:
        self.files[path] = data
        self.names.append(path)
        item = etree.SubElement(self.manifest, f"{{{OPF}}}item")
        item.set("id", item_id)
        item.set("href", posixpath.relpath(path, self.opf_dir or "."))
        item.set("media-type", media_type)
        if properties:
            item.set("properties", properties)
        return item

    def set_text(self, path: str, text: str) -> None:
        self.files[path] = text.encode("utf-8")
        self.changed.add(path)

    def text(self, path: str) -> str:
        data = self.files[path]
        return data.decode("utf-8-sig", errors="replace")

    def save(self) -> bytes:
        self.files[self.opf_path] = etree.tostring(self.opf, xml_declaration=True, encoding="utf-8")
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            # The mimetype entry must come first and be stored uncompressed.
            z.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
            for name in self.names:
                if name == "mimetype":
                    continue
                z.writestr(name, self.files[name], compress_type=zipfile.ZIP_DEFLATED)
        return out.getvalue()

    # -- metadata helpers ----------------------------------------------------------------------------------

    def dc(self, name: str) -> list:
        return self.metadata.findall(f"dc:{name}", NS)

    def meta_property(self, prop: str) -> list:
        return [m for m in self.metadata.findall("opf:meta", NS) if m.get("property") == prop]

    def meta_name(self, name: str):
        return next((m for m in self.metadata.findall("opf:meta", NS) if m.get("name") == name), None)

    def add_meta(self, prop: str, value: str) -> None:
        meta = etree.SubElement(self.metadata, f"{{{OPF}}}meta")
        if self.is_epub3:
            meta.set("property", prop)
            meta.text = value
        else:  # EPUB 2 has no property attribute: name/content pairs instead
            meta.set("name", prop)
            meta.set("content", value)

    def add_named_meta(self, name: str, content: str) -> None:
        meta = etree.SubElement(self.metadata, f"{{{OPF}}}meta")
        meta.set("name", name)
        meta.set("content", content)

    # -- book content --------------------------------------------------------------------------------------

    def parse_xhtml(self, path: str):
        parser = etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True, no_network=True)
        try:
            return etree.fromstring(self.files[path], parser)
        except (etree.XMLSyntaxError, ValueError):
            return None

    def text_sample(self, limit: int = 200_000) -> str:
        parts, size = [], 0
        for path in self.content_docs():
            root = self.parse_xhtml(path) if path in self.files else None
            if root is None:
                continue
            body = root.find(".//x:body", NS)
            text = " ".join((body if body is not None else root).itertext())
            parts.append(text)
            size += len(text)
            if size >= limit:
                break
        return " ".join(parts)

    def toc(self) -> list[TocEntry]:
        """The book's table of contents, from the navigation document or else the NCX."""
        nav = self.nav_item()
        if nav is not None and self.item_path(nav) in self.files:
            nav_path = self.item_path(nav)
            root = self.parse_xhtml(nav_path)
            if root is not None:
                for el in root.iter(f"{{{XHTML}}}nav"):
                    if "toc" in (el.get(f"{{{OPS}}}type") or "").split():
                        ol = el.find("x:ol", NS)
                        if ol is not None:
                            return self._nav_entries(ol, posixpath.dirname(nav_path))
        ncx = self.ncx_item()
        if ncx is not None and self.item_path(ncx) in self.files:
            ncx_path = self.item_path(ncx)
            root = etree.fromstring(self.files[ncx_path])
            nav_map = root.find("ncx:navMap", NS)
            if nav_map is not None:
                return self._ncx_entries(nav_map, posixpath.dirname(ncx_path))
        return []

    def _nav_entries(self, ol, base: str) -> list[TocEntry]:
        entries = []
        for li in ol.findall("x:li", NS):
            a = li.find("x:a", NS)
            label = a if a is not None else li.find("x:span", NS)
            title = " ".join("".join(label.itertext()).split()) if label is not None else ""
            href = a.get("href", "") if a is not None else ""
            sub = li.find("x:ol", NS)
            entries.append(
                TocEntry(title, self.resolve(base, href), href.split("#", 1)[1] if "#" in href else "",
                         self._nav_entries(sub, base) if sub is not None else [])
            )
        return entries

    def _ncx_entries(self, parent, base: str) -> list[TocEntry]:
        entries = []
        for point in parent.findall("ncx:navPoint", NS):
            title = " ".join((point.findtext("ncx:navLabel/ncx:text", namespaces=NS) or "").split())
            content = point.find("ncx:content", NS)
            src = content.get("src", "") if content is not None else ""
            entries.append(
                TocEntry(title, self.resolve(base, src), src.split("#", 1)[1] if "#" in src else "", self._ncx_entries(point, base))
            )
        return entries

    def cover_item(self):
        """The cover image: marked with cover-image, named by <meta name="cover">, or called "cover"."""
        marked = next((i for i in self.items() if "cover-image" in self.properties(i)), None)
        if marked is not None:
            return marked
        meta = self.meta_name("cover")
        if meta is not None:
            item = self.item(meta.get("content", ""))
            if item is not None and (item.get("media-type") or "").startswith("image/"):
                return item
        return next(
            (i for i in self.items() if (i.get("media-type") or "").startswith("image/") and "cover" in (i.get("id", "") + i.get("href", "")).lower()),
            None,
        )


def _flatten(entries: list[TocEntry]) -> list[TocEntry]:
    out = []
    for e in entries:
        out.append(e)
        out += _flatten(e.children)
    return out


def _detect_language(text: str) -> str | None:
    counts = script_counts(text)
    total = sum(counts.values())
    if total < 200:
        return None
    script, n = counts.most_common(1)[0]
    return language_for_script(script) if n / total >= 0.6 else None


def _language_mismatch(declared: str, text: str) -> bool:
    counts = script_counts(text)
    total = sum(counts.values())
    if total < 200:
        return False
    script, n = counts.most_common(1)[0]
    if n / total < 0.6:
        return False
    expected = _LANG_SCRIPTS.get(declared.split("-")[0].lower(), {"LATIN"})
    return script not in expected


def _has_contents_page(pkg: EpubPackage, toc: list[TocEntry]) -> bool:
    """A visible contents page exists: the navigation document is in the reading order, or a chapter links
    to most of the documents the table of contents points to."""
    if any((i.get("id") or "").startswith(CONTENTS_ID) for i in pkg.items()):
        return True  # added by an earlier run
    nav = pkg.nav_item()
    for ref, item in pkg.spine_items():
        if nav is not None and item is nav and ref.get("linear", "yes") != "no":
            return True
    targets = {e.path for e in _flatten(toc)}
    if len(targets) < 2:
        return False
    for path in pkg.content_docs():
        if path not in pkg.files:
            continue
        hrefs = set(re.findall(r'href="([^"#]+)', pkg.text(path)))
        linked = {pkg.resolve(posixpath.dirname(path), h) for h in hrefs}
        if path not in targets and len(linked & targets) >= max(2, 0.6 * len(targets)):
            return True
    return False


def _has_landmarks(pkg: EpubPackage) -> bool:
    nav = pkg.nav_item()
    if nav is None or pkg.item_path(nav) not in pkg.files:
        return False
    return 'epub:type="landmarks"' in pkg.text(pkg.item_path(nav)).replace("'", '"')


def _rtl_ok(pkg: EpubPackage, direction: str) -> bool:
    if direction != "rtl":
        return True
    ppd = pkg.spine.get("page-progression-direction") == "rtl" or not pkg.is_epub3
    return ppd and pkg.meta_name("primary-writing-mode") is not None


def inspect_epub(data: bytes) -> EpubInfo:
    pkg = EpubPackage(data)
    toc = pkg.toc()
    text = pkg.text_sample()
    declared = (pkg.dc("language")[0].text or "").strip() if pkg.dc("language") else None
    detected = _detect_language(text)
    language = detected if declared and _language_mismatch(declared, text) else (declared or detected)
    direction = direction_for_language(language or "en")
    cover = pkg.cover_item()
    info = EpubInfo(
        version=pkg.version,
        title=(pkg.dc("title")[0].text or "").strip() if pkg.dc("title") else "",
        authors=[(c.text or "").strip() for c in pkg.dc("creator") if (c.text or "").strip()],
        language=declared,
        detected_language=detected,
        direction=direction,
        has_nav=pkg.nav_item() is not None,
        has_ncx=pkg.ncx_item() is not None,
        has_contents_page=_has_contents_page(pkg, toc),
        has_landmarks=_has_landmarks(pkg),
        has_cover=cover is not None,
        cover_marked=cover is not None and "cover-image" in pkg.properties(cover),
        has_accessibility=bool(pkg.meta_property("schema:accessMode")) or pkg.meta_name("schema:accessMode") is not None,
        rtl_ok=_rtl_ok(pkg, direction),
        chapters=len(pkg.content_docs()),
        toc_entries=len(_flatten(toc)),
        words=len(text.split()),
    )
    s = info.suggestions
    if not pkg.is_epub3:
        s.append("Upgrade from EPUB 2 to EPUB 3 (adds a navigation document)")
    if not declared:
        s.append(f"Set the missing language ({detected or 'unknown'})")
    elif _language_mismatch(declared, text):
        s.append(f"Fix the language: tagged '{declared}' but the text is '{detected}'")
    if not info.rtl_ok:
        s.append("Set right-to-left page turning and Kindle's writing mode")
    if info.toc_entries >= 2 and not info.has_contents_page:
        s.append("Add a contents page")
    if not info.has_landmarks:
        s.append("Add navigation landmarks")
    if not info.has_accessibility:
        s.append("Add accessibility metadata")
    if not info.has_cover:
        s.append("No cover image: you can add one")
    elif not info.cover_marked:
        s.append("Mark the cover image so readers and Kindle show it")
    return info


# ---------------------------------------------------------------------------------------------------------
# Enrichment steps


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _upgrade_to_epub3(pkg: EpubPackage, changes: list[str]) -> None:
    pkg.opf.set("version", "3.0")
    # EPUB 2 attributes on Dublin Core elements are not allowed in EPUB 3: turn roles and sort names into
    # refining meta elements.
    opf_attrs = [f"{{{OPF}}}role", f"{{{OPF}}}file-as", f"{{{OPF}}}scheme", f"{{{OPF}}}event"]
    for el in list(pkg.metadata):
        found = {a: el.get(a) for a in opf_attrs if el.get(a)}
        if not found:
            continue
        for a in found:
            del el.attrib[a]
        if el.get("id") is None:
            el.set("id", f"meta-{uuid.uuid4().hex[:8]}")
        for attr, value in found.items():
            local = attr.split("}")[1]
            if local in ("role", "file-as"):
                meta = etree.SubElement(pkg.metadata, f"{{{OPF}}}meta")
                meta.set("refines", f"#{el.get('id')}")
                meta.set("property", local)
                if local == "role":
                    meta.set("scheme", "marc:relators")
                meta.text = value
    # A navigation document built from the NCX.
    if pkg.nav_item() is None:
        toc = pkg.toc()
        nav_path = pkg.unique_path(posixpath.join(pkg.opf_dir, "nav.xhtml"))
        pkg.add_file(nav_path, b"", "application/xhtml+xml", pkg.unique_id(NAV_ID), "nav")
        pkg.set_text(nav_path, _nav_document(pkg, toc, nav_path))
        changes.append("Upgraded EPUB 2 to EPUB 3 with a navigation document")
    else:
        changes.append("Upgraded EPUB 2 to EPUB 3")
    # Content documents: HTML5 doctype (an XHTML 1.1 doctype is an error in EPUB 3), HTML4 leftovers that
    # HTML5 rejects, and the manifest properties EPUB 3 requires for SVG and scripts.
    for item in pkg.items():
        if "html" not in (item.get("media-type") or ""):
            continue
        path = pkg.item_path(item)
        if path not in pkg.files:
            continue
        text = pkg.text(path)
        new = _DOCTYPE.sub("<!DOCTYPE html>", text, count=1)
        if 'xmlns:epub=' not in new:
            new = _HTML_TAG.sub(lambda m: m.group(0)[:-1].rstrip("/") + f' xmlns:epub="{OPS}">', new, count=1)
        new = _TABLE_SUMMARY.sub(r"\1", new)
        new = _fix_content_type_meta(new)
        if new != text:
            pkg.set_text(path, new)
        props = pkg.properties(item)
        if re.search(r"<(svg:)?svg\b", new):
            props.add("svg")
        if re.search(r"<script\b", new, re.IGNORECASE):
            props.add("scripted")
        if props != pkg.properties(item):
            item.set("properties", " ".join(sorted(props)))
    # The cover image named the EPUB 2 way gets the EPUB 3 property.
    cover = pkg.cover_item()
    if cover is not None and "cover-image" not in pkg.properties(cover):
        cover.set("properties", " ".join(sorted(pkg.properties(cover) | {"cover-image"})))


def _nav_document(pkg: EpubPackage, toc: list[TocEntry], nav_path: str) -> str:
    base = posixpath.dirname(nav_path)
    lang = (pkg.dc("language")[0].text or "en") if pkg.dc("language") else "en"

    def ol(entries: list[TocEntry]) -> str:
        rows = []
        for e in entries:
            href = posixpath.relpath(e.path, base or ".") + (f"#{e.fragment}" if e.fragment else "")
            sub = ol(e.children) if e.children else ""
            rows.append(f'<li><a href="{html.escape(href)}">{html.escape(e.title)}</a>{sub}</li>')
        return f"<ol>{''.join(rows)}</ol>"

    if not toc:
        first = pkg.content_docs()[0] if pkg.content_docs() else ""
        toc = [TocEntry((pkg.dc("title")[0].text or "Start") if pkg.dc("title") else "Start", first)]
    direction = direction_for_language(lang)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        f'<html xmlns="{XHTML}" xmlns:epub="{OPS}" lang="{lang}" xml:lang="{lang}" dir="{direction}">\n'
        f"<head><title>{html.escape(contents_label(lang))}</title></head>\n<body>\n"
        f'<nav epub:type="toc" id="toc"><h1>{html.escape(contents_label(lang))}</h1>{ol(toc)}</nav>\n'
        "</body>\n</html>\n"
    )


def _set_language(pkg: EpubPackage, language: str, changes: list[str]) -> None:
    langs = pkg.dc("language")
    if langs:
        old = langs[0].text
        if old == language:
            return
        langs[0].text = language
        changes.append(f"Language: {old} → {language}")
    else:
        el = etree.SubElement(pkg.metadata, f"{{{DC}}}language")
        el.text = language
        changes.append(f"Language set to {language}")


def _set_title_authors(pkg: EpubPackage, opts: EnrichOptions, changes: list[str]) -> None:
    if opts.title:
        titles = pkg.dc("title")
        if titles:
            if titles[0].text != opts.title:
                titles[0].text = opts.title
                changes.append(f"Title: {opts.title}")
        else:
            etree.SubElement(pkg.metadata, f"{{{DC}}}title").text = opts.title
            changes.append(f"Title: {opts.title}")
    if opts.authors is not None:
        current = [(c.text or "").strip() for c in pkg.dc("creator")]
        if current != opts.authors:
            for c in pkg.dc("creator"):
                if c.get("id"):  # drop refinements of the removed creators
                    for m in pkg.metadata.findall("opf:meta", NS):
                        if m.get("refines") == f"#{c.get('id')}":
                            pkg.metadata.remove(m)
                pkg.metadata.remove(c)
            for name in opts.authors:
                etree.SubElement(pkg.metadata, f"{{{DC}}}creator").text = name
            changes.append(f"Authors: {', '.join(opts.authors) or '(none)'}")


def _apply_direction(pkg: EpubPackage, language: str, changes: list[str]) -> None:
    direction = direction_for_language(language)
    if direction == "rtl":
        if pkg.is_epub3 and pkg.spine.get("page-progression-direction") != "rtl":
            pkg.spine.set("page-progression-direction", "rtl")
            changes.append("Right-to-left page progression")
        if pkg.meta_name("primary-writing-mode") is None:
            pkg.add_named_meta("primary-writing-mode", "horizontal-rl")
            changes.append("Kindle right-to-left writing mode")
    fixed = 0
    for path in pkg.content_docs():
        if path not in pkg.files:
            continue
        text = pkg.text(path)
        m = _HTML_TAG.search(text)
        if not m:
            continue
        tag = m.group(0)
        attrs = ""
        if not re.search(r"\sxml:lang=", tag):
            attrs += f' xml:lang="{language}"'
        if not re.search(r"\slang=", tag):
            attrs += f' lang="{language}"'
        body_dir = re.search(r"<body\b[^>]*\sdir=", text, re.IGNORECASE)
        if direction == "rtl" and not re.search(r"\sdir=", tag) and not body_dir:
            attrs += ' dir="rtl"'
        if attrs:
            new_tag = tag[:-1].rstrip("/").rstrip() + attrs + ">"
            pkg.set_text(path, text[: m.start()] + new_tag + text[m.end():])
            fixed += 1
    if fixed:
        changes.append(f"Language/direction set on {fixed} chapter files")


def _stylesheets(pkg: EpubPackage, doc_path: str) -> list[str]:
    """Stylesheet links of a content document, resolved to zip paths."""
    if doc_path not in pkg.files:
        return []
    base = posixpath.dirname(doc_path)
    links = re.findall(r"<link\b[^>]*>", pkg.text(doc_path), re.IGNORECASE)
    out = []
    for link in links:
        if "stylesheet" in link.lower():
            href = re.search(r'href=["\']([^"\']+)', link)
            if href:
                out.append(pkg.resolve(base, href.group(1)))
    return out


def _body_start(pkg: EpubPackage, toc: list[TocEntry]) -> str | None:
    """The document where the text proper begins: the first contents entry that isn't front matter."""
    docs = pkg.content_docs()
    for e in _flatten(toc):
        if e.path in docs and not _FRONT_MATTER.search(e.title):
            return e.path
    return docs[0] if docs else None


def _add_contents_page(pkg: EpubPackage, toc: list[TocEntry], language: str, changes: list[str]) -> str | None:
    start = _body_start(pkg, toc)
    if start is None:
        return None
    base = posixpath.dirname(start)
    path = pkg.unique_path(posixpath.join(base, "pdf2epub-contents.xhtml"))
    label = contents_label(language)
    direction = direction_for_language(language)
    start_side = "right" if direction == "rtl" else "left"

    def ol(entries: list[TocEntry]) -> str:
        rows = []
        for e in entries:
            href = posixpath.relpath(e.path, base or ".") + (f"#{e.fragment}" if e.fragment else "")
            sub = ol(e.children) if e.children else ""
            rows.append(f'<li><a href="{html.escape(href)}">{html.escape(e.title)}</a>{sub}</li>')
        return f"<ol>{''.join(rows)}</ol>"

    css = "".join(
        f'<link rel="stylesheet" type="text/css" href="{html.escape(posixpath.relpath(s, base or "."))}"/>'
        for s in _stylesheets(pkg, start)
    )
    style = (
        "<style>.pdf2epub-contents ol{list-style:none;margin:0;padding:0}"
        ".pdf2epub-contents li{margin:.45em 0}"
        f".pdf2epub-contents li ol{{padding-{start_side}:1.5em}}"
        ".pdf2epub-contents a{text-decoration:none}.pdf2epub-contents h1{text-align:center}</style>"
    )
    if pkg.is_epub3:
        doc = (
            '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            f'<html xmlns="{XHTML}" xmlns:epub="{OPS}" lang="{language}" xml:lang="{language}" dir="{direction}">\n'
            f"<head><title>{html.escape(label)}</title>{css}{style}</head>\n"
            f'<body><section class="pdf2epub-contents" epub:type="frontmatter"><h1>{html.escape(label)}</h1>{ol(toc)}</section></body>\n</html>\n'
        )
    else:
        style2 = style.replace("<style>", '<style type="text/css">')
        doc = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
            f'<html xmlns="{XHTML}" xml:lang="{language}" dir="{direction}">\n'
            f"<head><title>{html.escape(label)}</title>{css}{style2}</head>\n"
            f'<body><div class="pdf2epub-contents"><h1>{html.escape(label)}</h1>{ol(toc)}</div></body>\n</html>\n'
        )
    item_id = pkg.unique_id(CONTENTS_ID)
    pkg.add_file(path, doc.encode("utf-8"), "application/xhtml+xml", item_id)
    # In the reading order just before the text proper.
    start_item = pkg.item_by_path(start)
    ref = etree.Element(f"{{{OPF}}}itemref")
    ref.set("idref", item_id)
    for existing in pkg.spine.findall("opf:itemref", NS):
        if start_item is not None and existing.get("idref") == start_item.get("id"):
            existing.addprevious(ref)
            break
    else:
        pkg.spine.append(ref)
    if not pkg.is_epub3:
        guide = pkg.opf.find("opf:guide", NS)
        if guide is None:
            guide = etree.SubElement(pkg.opf, f"{{{OPF}}}guide")
        if not any(r.get("type") == "toc" for r in guide.findall("opf:reference", NS)):
            r = etree.SubElement(guide, f"{{{OPF}}}reference")
            r.set("type", "toc")
            r.set("title", label)
            r.set("href", posixpath.relpath(path, pkg.opf_dir or "."))
    changes.append(f"Added a contents page ({len(_flatten(toc))} entries)")
    return path


def _add_landmarks(pkg: EpubPackage, toc: list[TocEntry], contents_path: str | None, language: str, changes: list[str]) -> None:
    nav = pkg.nav_item()
    if nav is None or not pkg.is_epub3:
        return
    nav_path = pkg.item_path(nav)
    if nav_path not in pkg.files or _has_landmarks(pkg):
        return
    base = posixpath.dirname(nav_path)
    rows = []
    if contents_path:
        rows.append(f'<li><a epub:type="toc" href="{html.escape(posixpath.relpath(contents_path, base or "."))}">{html.escape(contents_label(language))}</a></li>')
    start = _body_start(pkg, toc)
    if start:
        rows.append(f'<li><a epub:type="bodymatter" href="{html.escape(posixpath.relpath(start, base or "."))}">Start</a></li>')
    if not rows:
        return
    block = f'<nav epub:type="landmarks" hidden="hidden"><ol>{"".join(rows)}</ol></nav>'
    text = pkg.text(nav_path)
    idx = text.lower().rfind("</body>")
    if idx < 0:
        return
    if "xmlns:epub=" not in text:
        text = _HTML_TAG.sub(lambda m: m.group(0)[:-1].rstrip("/") + f' xmlns:epub="{OPS}">', text, count=1)
        idx = text.lower().rfind("</body>")
    pkg.set_text(nav_path, text[:idx] + block + "\n" + text[idx:])
    changes.append("Added navigation landmarks")


def _add_accessibility(pkg: EpubPackage, changes: list[str]) -> None:
    if pkg.meta_property("schema:accessMode") or pkg.meta_name("schema:accessMode") is not None:
        return
    has_images = any((i.get("media-type") or "").startswith("image/") for i in pkg.items())
    for prop, value in accessibility_metadata(has_images, ocr=False):
        pkg.add_meta(prop, value)
    changes.append("Added accessibility metadata")


def _set_cover(pkg: EpubPackage, image: bytes, changes: list[str]) -> None:
    ext, media = ("png", "image/png") if image[:8] == b"\x89PNG\r\n\x1a\n" else ("jpg", "image/jpeg")
    for item in pkg.items():
        props = pkg.properties(item) - {"cover-image"}
        if props != pkg.properties(item):
            if props:
                item.set("properties", " ".join(sorted(props)))
            else:
                del item.attrib["properties"]
    path = pkg.unique_path(posixpath.join(pkg.opf_dir, f"images/pdf2epub-cover.{ext}"))
    item_id = pkg.unique_id(COVER_ID)
    pkg.add_file(path, image, media, item_id, "cover-image" if pkg.is_epub3 else "")
    meta = pkg.meta_name("cover")
    if meta is not None:
        meta.set("content", item_id)
    else:
        pkg.add_named_meta("cover", item_id)
    changes.append("Set a new cover image")


def _mark_cover(pkg: EpubPackage, changes: list[str]) -> None:
    cover = pkg.cover_item()
    if cover is None:
        return
    if pkg.is_epub3 and "cover-image" not in pkg.properties(cover):
        cover.set("properties", " ".join(sorted(pkg.properties(cover) | {"cover-image"})))
        changes.append("Marked the cover image")
    if pkg.meta_name("cover") is None:  # Kindle still reads the EPUB 2 style marker
        pkg.add_named_meta("cover", cover.get("id"))
        if not changes or changes[-1] != "Marked the cover image":
            changes.append("Marked the cover image")


def _fix_ocr_text(pkg: EpubPackage, language: str, changes: list[str]) -> None:
    from .spell import Corrector

    corrector = Corrector(language)
    if not corrector.enabled:
        return
    tag = re.compile(r"(<[^>]+>)")
    for path in pkg.content_docs():
        if path not in pkg.files:
            continue
        text = pkg.text(path)
        parts = tag.split(text)
        in_skip = False
        for i, part in enumerate(parts):
            if i % 2:  # a tag
                low = part.lower()
                if low.startswith(("<script", "<style")):
                    in_skip = True
                elif low.startswith(("</script", "</style")):
                    in_skip = False
                continue
            if not in_skip and part.strip():
                parts[i] = corrector.fix(part)
        new = "".join(parts)
        if new != text:
            pkg.set_text(path, new)
    fixed = sum(corrector.fixed.values())
    if fixed:
        changes.append(f"Fixed {fixed} OCR letter confusions")


def enrich_epub(data: bytes, opts: EnrichOptions | None = None) -> tuple[bytes, list[str]]:
    """Return the enriched EPUB and a list of the changes made."""
    opts = opts or EnrichOptions()
    pkg = EpubPackage(data)
    changes: list[str] = []
    if opts.upgrade and not pkg.is_epub3:
        _upgrade_to_epub3(pkg, changes)

    text = pkg.text_sample()
    declared = (pkg.dc("language")[0].text or "").strip() if pkg.dc("language") else ""
    language = opts.language or declared or _detect_language(text) or "en"
    if not opts.language and opts.fix_language and declared and _language_mismatch(declared, text):
        language = _detect_language(text) or declared
    if opts.language or opts.fix_language or not declared:
        _set_language(pkg, language, changes)
    _set_title_authors(pkg, opts, changes)

    if opts.direction:
        _apply_direction(pkg, language, changes)
    if opts.cover_image:
        _set_cover(pkg, opts.cover_image, changes)
    elif opts.cover:
        _mark_cover(pkg, changes)

    toc = pkg.toc()
    contents_path = None
    if opts.contents_page and len(_flatten(toc)) >= 2 and not _has_contents_page(pkg, toc):
        contents_path = _add_contents_page(pkg, toc, language, changes)
    if opts.landmarks:
        _add_landmarks(pkg, toc, contents_path, language, changes)
    if opts.accessibility:
        _add_accessibility(pkg, changes)
    if opts.ocr_fixes:
        _fix_ocr_text(pkg, language, changes)

    if changes and pkg.is_epub3:
        modified = pkg.meta_property("dcterms:modified")
        if modified:
            modified[0].text = _now()
        else:
            pkg.add_meta("dcterms:modified", _now())
    if pkg.is_epub3 and not pkg.meta_property("dcterms:modified"):
        pkg.add_meta("dcterms:modified", _now())
    return pkg.save(), changes
