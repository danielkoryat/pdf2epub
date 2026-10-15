"""Writes a Book as an EPUB 3 file that also works well after Kindle's Send-to-Kindle conversion."""

from __future__ import annotations

import html
import re
import uuid
from pathlib import Path

from ebooklib import epub

from .cleanup import SCENE_BREAK
from .lang import base_direction
from .model import NOTE_CLOSE, NOTE_OPEN, Block, Book, Chapter, Kind

_NOTE_REF = re.compile(f"{NOTE_OPEN}(.*?){NOTE_CLOSE}")
_MEDIA_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif"}

CSS = """\
@namespace epub "http://www.idpf.org/2007/ops";
body { margin: 0; padding: 0 0.3em; line-height: 1.45; widows: 2; orphans: 2; }
p { margin: 0; text-indent: 1.4em; text-align: justify; }
p.first, p.scene-break, p.caption, .notes p, li p { text-indent: 0; }
h1, h2, h3, h4, h5, h6 { text-align: center; text-indent: 0; font-weight: bold; page-break-after: avoid; hyphens: none; }
h1 { font-size: 1.5em; margin: 2.5em 0 0.4em; }
h2 { font-size: 1.25em; margin: 0.4em 0 1.4em; }
h1 + p, h2 + p { margin-top: 1.4em; }
h3 { font-size: 1.1em; margin: 1.2em 0 0.6em; }
h4, h5, h6 { font-size: 1em; margin: 1em 0 0.5em; }
p.scene-break { text-align: center; margin: 0.8em 0; }
figure { margin: 1em 0; text-align: center; page-break-inside: avoid; }
figure img { max-width: 100%; height: auto; }
figure.full-page img { max-height: 95vh; }
figcaption, p.caption { font-size: 0.9em; font-style: italic; text-align: center; margin: 0.3em 0 1em; }
table { border-collapse: collapse; margin: 1em auto; font-size: 0.9em; }
td, th { border: 1px solid #888; padding: 0.2em 0.4em; vertical-align: top; }
pre { white-space: pre-wrap; font-size: 0.85em; text-align: left; }
ul, ol { margin: 0.5em 0; padding-START: 1.6em; padding-END: 0; }
a.noteref { text-decoration: none; vertical-align: super; font-size: 0.75em; line-height: 0; }
section.notes { margin-top: 2.5em; border-top: 1px solid #999; padding-top: 0.6em; font-size: 0.85em; }
aside.footnote { margin: 0 0 0.5em; }
aside.footnote a.backlink { text-decoration: none; font-weight: bold; }
section.contents h1 { margin: 1.5em 0 1.2em; }
section.contents ol { list-style: none; margin: 0; padding: 0; }
section.contents li { margin: 0.45em 0; }
section.contents li ol { padding-START: 1.5em; }
section.contents li li { margin: 0.25em 0; }
section.contents a { text-decoration: none; }
.cover-image { text-align: center; margin: 0; padding: 0; }
.cover-image img { max-width: 100%; max-height: 100%; }
"""


def build_css(direction: str) -> str:
    start, end = ("right", "left") if direction == "rtl" else ("left", "right")
    return CSS.replace("START", start).replace("END", end)


def _inline(text: str, notes: dict[str, Block], files: dict[str, str], here: str) -> str:
    """Escape text and turn footnote placeholders into noteref links."""

    def ref(m: re.Match[str]) -> str:
        note_id = m.group(1)
        note = notes.get(note_id)
        marker = html.escape(note.marker if note else "*")
        target = files.get(note_id, here)
        href = f"#{note_id}" if target == here else f"{target}#{note_id}"
        return f'<a epub:type="noteref" class="noteref" id="ref-{note_id}" href="{href}">{marker}</a>'

    return _NOTE_REF.sub(ref, html.escape(text, quote=False))


def _dir_attr(text: str, book_dir: str) -> str:
    d = base_direction(text)
    return f' dir="{d}"' if d and d != book_dir else ""


class _ChapterRenderer:
    def __init__(self, book: Book, notes: dict[str, Block], note_files: dict[str, str], images: dict[int, str]):
        self.book = book
        self.notes = notes
        self.note_files = note_files
        self.images = images  # id(block) -> image path relative to the chapter

    def render(self, ch: Chapter, file_name: str) -> str:
        out: list[str] = []
        blocks = ch.blocks
        opening = 0
        while opening < len(blocks) and blocks[opening].kind is Kind.HEADING and ch.in_toc:
            opening += 1
        prev = "start"
        list_tag = ""
        i = 0
        while i < len(blocks):
            b = blocks[i]
            if list_tag and b.kind is not Kind.LIST_ITEM:
                out.append(f"</{list_tag}>")
                list_tag = ""
            text = _inline(b.text, self.notes, self.note_files, file_name) if b.text else ""
            dir_attr = _dir_attr(b.text, self.book.direction)
            if b.kind is Kind.HEADING:
                if i < opening:
                    tag = "h1" if i == 0 else "h2"
                else:
                    tag = f"h{min(max(b.level + 1, 2), 6)}"
                out.append(f"<{tag}{dir_attr}>{text}</{tag}>")
                prev = "heading"
            elif b.kind is Kind.PARAGRAPH:
                if SCENE_BREAK.match(b.text):
                    out.append(f'<p class="scene-break">{text}</p>')
                    prev = "break"
                else:
                    cls = ' class="first"' if prev != "paragraph" else ""
                    out.append(f"<p{cls}{dir_attr}>{text}</p>")
                    prev = "paragraph"
            elif b.kind is Kind.LIST_ITEM:
                tag = "ol" if b.enumerated else "ul"
                if list_tag != tag:
                    if list_tag:
                        out.append(f"</{list_tag}>")
                    out.append(f"<{tag}>")
                    list_tag = tag
                out.append(f"<li{dir_attr}>{text}</li>")
                prev = "list"
            elif b.kind is Kind.IMAGE:
                caption = alt = ""
                if i + 1 < len(blocks) and blocks[i + 1].kind is Kind.CAPTION:
                    i += 1
                    cap = blocks[i]
                    caption = f"<figcaption{_dir_attr(cap.text, self.book.direction)}>{_inline(cap.text, self.notes, self.note_files, file_name)}</figcaption>"
                    alt = html.escape(_NOTE_REF.sub("", cap.text), quote=True)
                cls = ' class="full-page"' if b.height > 1.3 * b.width else ""
                out.append(f'<figure{cls}><img src="{self.images[id(b)]}" alt="{alt}"/>{caption}</figure>')
                prev = "image"
            elif b.kind is Kind.CAPTION:
                out.append(f'<p class="caption"{dir_attr}>{text}</p>')
                prev = "caption"
            elif b.kind is Kind.TABLE:
                out.append(b.html)
                prev = "table"
            elif b.kind is Kind.CODE:
                # Code reads left to right in any book (the CSS 'direction' property is not allowed in EPUB).
                out.append(f'<pre dir="ltr">{html.escape(b.text, quote=False)}</pre>')
                prev = "code"
            i += 1
        if list_tag:
            out.append(f"</{list_tag}>")
        if ch.notes:
            out.append('<section class="notes" epub:type="footnotes">')
            for note in ch.notes:
                body = _inline(note.text, self.notes, self.note_files, file_name)
                if note.linked:
                    back = f'<a class="backlink" href="#ref-{note.note_id}">{html.escape(note.marker)}</a> '
                else:
                    back = ""
                out.append(
                    f'<aside epub:type="footnote" class="footnote" id="{note.note_id}">'
                    f"<p{_dir_attr(note.text, self.book.direction)}>{back}{body}</p></aside>"
                )
            out.append("</section>")
        opening_tag = '<section epub:type="chapter">' if ch.in_toc else "<section>"
        return opening_tag + "\n".join(out) + "</section>"


CONTENTS_LABELS = {
    "he": "תוכן העניינים",
    "en": "Contents",
    "ar": "المحتويات",
    "fa": "فهرست",
    "yi": "אינהאַלט",
    "fr": "Table des matières",
    "de": "Inhalt",
    "es": "Índice",
    "it": "Indice",
    "pt": "Sumário",
    "nl": "Inhoud",
    "ru": "Содержание",
    "uk": "Зміст",
    "pl": "Spis treści",
}


def contents_label(language: str) -> str:
    return CONTENTS_LABELS.get(language.split("-")[0].lower(), "Contents")


def _contents_page(book: Book, entries: list[tuple[epub.Link, list[epub.Link]]]) -> epub.EpubHtml:
    def link(entry: epub.Link) -> str:
        href = Path(entry.href).name  # the contents page lives in text/ next to the chapters
        return f'<a href="{href}"{_dir_attr(entry.title, book.direction)}>{html.escape(entry.title, quote=False)}</a>'

    rows = []
    for entry, children in entries:
        sub = "".join(f"<li>{link(child)}</li>" for child in children)
        rows.append(f"<li>{link(entry)}{f'<ol>{sub}</ol>' if sub else ''}</li>")
    label = contents_label(book.language)
    page = epub.EpubHtml(uid="contents", file_name="text/contents.xhtml", title=label, lang=book.language, direction=book.direction)
    page.content = f'<section class="contents"><h1>{html.escape(label)}</h1><ol>{"".join(rows)}</ol></section>'
    page.add_link(href="../styles/book.css", rel="stylesheet", type="text/css")
    return page


def accessibility_metadata(has_images: bool, ocr: bool = True) -> list[tuple[str, str]]:
    """schema.org accessibility metadata (EPUB Accessibility 1.1), as reading systems and stores expect."""
    summary = "Reflowable text with a navigable table of contents and headings; reading order follows the original book."
    if ocr:
        summary += " Text of scanned pages was produced by OCR and may contain recognition errors."
    return [
        ("schema:accessMode", "textual"),
        *([("schema:accessMode", "visual")] if has_images else []),
        ("schema:accessModeSufficient", "textual"),
        ("schema:accessibilityFeature", "tableOfContents"),
        ("schema:accessibilityFeature", "readingOrder"),
        ("schema:accessibilityFeature", "structuralNavigation"),
        ("schema:accessibilityHazard", "none"),
        ("schema:accessibilitySummary", summary),
    ]


def _add_accessibility_metadata(eb: epub.EpubBook, has_images: bool) -> None:
    for prop, value in accessibility_metadata(has_images):
        eb.add_metadata(None, "meta", value, {"property": prop})


def write_epub(book: Book, path: Path) -> None:
    eb = epub.EpubBook()
    eb.set_identifier(f"urn:uuid:{uuid.uuid4()}")
    eb.set_title(book.title)
    eb.set_language(book.language)
    for author in book.authors:
        eb.add_author(author)
    if book.direction == "rtl":
        eb.set_direction("rtl")  # spine page-progression-direction
        # Kindle reads this to turn pages right-to-left.
        eb.add_metadata(None, "meta", "", {"name": "primary-writing-mode", "content": "horizontal-rl"})

    css = epub.EpubItem(uid="style", file_name="styles/book.css", media_type="text/css", content=build_css(book.direction))
    eb.add_item(css)

    if book.cover:
        eb.set_cover(f"images/cover.{book.cover_ext}", book.cover, create_page=True)

    # Images and file names first, so footnote links can point across files.
    images: dict[int, str] = {}
    n_img = 0
    file_names = [f"text/ch{n:03d}.xhtml" for n in range(1, len(book.chapters) + 1)]
    notes: dict[str, Block] = {}
    note_files: dict[str, str] = {}
    for ch, name in zip(book.chapters, file_names):
        for note in ch.notes:
            notes[note.note_id] = note
            note_files[note.note_id] = Path(name).name
        for b in ch.blocks:
            if b.kind is Kind.IMAGE and b.image:
                n_img += 1
                img_name = f"images/img{n_img:04d}.{b.image_ext}"
                eb.add_item(epub.EpubImage(uid=f"img{n_img:04d}", file_name=img_name, media_type=_MEDIA_TYPES[b.image_ext], content=b.image))
                images[id(b)] = f"../{img_name}"

    renderer = _ChapterRenderer(book, notes, note_files, images)
    items: list[epub.EpubHtml] = []
    entries: list[tuple[epub.Link, list[epub.Link]]] = []  # top-level entries with their sub-entries
    for n, (ch, name) in enumerate(zip(book.chapters, file_names), start=1):
        title = ch.title or book.title
        item = epub.EpubHtml(uid=f"ch{n:03d}", file_name=name, title=title, lang=book.language, direction=book.direction)
        item.content = renderer.render(ch, Path(name).name)
        # ebooklib's add_item(css) would link "styles/book.css", which is wrong from inside text/.
        item.add_link(href="../styles/book.css", rel="stylesheet", type="text/css")
        eb.add_item(item)
        items.append(item)
        if ch.in_toc and ch.title:
            link = epub.Link(name, ch.title, f"toc{n:03d}")
            if ch.level > 1 and entries:
                entries[-1][1].append(link)
            else:
                entries.append((link, []))
    if not entries and items:
        entries.append((epub.Link(items[0].file_name, book.title, "toc001"), []))
    eb.toc = [(epub.Section(link.title, link.href), children) if children else link for link, children in entries]

    eb.add_item(epub.EpubNcx())
    nav = epub.EpubNav(direction=book.direction)
    nav.add_item(css)
    eb.add_item(nav)

    first_body = next((i for i, ch in enumerate(book.chapters) if ch.in_toc), 0)
    spine_items: list = list(items)
    toc_href = "nav.xhtml"
    if sum(1 + len(children) for _link, children in entries) >= 2:
        # A contents page in the reading order, after the front matter -- what printed books and
        # Kindle's publishing guidelines expect, in addition to the reader's own navigation menu.
        contents = _contents_page(book, entries)
        eb.add_item(contents)
        spine_items.insert(first_body, contents)
        toc_href = contents.file_name
    # Without a contents page, the navigation document stays reachable as a non-linear spine item.
    eb.spine = (["cover"] if book.cover else []) + spine_items + ([("nav", "no")] if toc_href == "nav.xhtml" else [])
    if book.cover:
        eb.guide.append({"href": "cover.xhtml", "title": "Cover", "type": "cover"})
    eb.guide.append({"href": toc_href, "title": contents_label(book.language), "type": "toc"})
    if items:
        eb.guide.append({"href": items[first_body].file_name, "title": "Start", "type": "text"})
    _add_accessibility_metadata(eb, has_images=bool(images) or bool(book.cover))

    path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(path), eb, {"epub3_pages": False})
