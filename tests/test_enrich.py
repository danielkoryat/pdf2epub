import io
import zipfile
from pathlib import Path

from pdf2epub.edit import ChapterEdit, Edits, apply_edits
from pdf2epub.enrich import EnrichOptions, EpubPackage, enrich_epub, inspect_epub
from pdf2epub.model import NOTE_CLOSE, NOTE_OPEN, Block, Book, Chapter, Kind
from pdf2epub.validate import check_epub, epubcheck_available

HEB = "היום מתה אמא. ואולי אתמול, אני לא יודע. קיבלתי מברק מהמעון ובו נאמר כי אמך נפטרה והלוויה מחר. " * 6
ENG = "It was a bright cold day in April, and the clocks were striking thirteen. " * 6
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def _zip(files: dict[str, str | bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        for name, data in files.items():
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    return out.getvalue()


def epub2(lang="en", text=ENG) -> bytes:
    """An EPUB 2 book: NCX only, XHTML 1.1 doctype, cover marked the EPUB 2 way, an HTML4 table summary."""
    xhtml = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
             '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n<html xmlns="http://www.w3.org/1999/xhtml"><head>'
             '<meta content="text/html; charset=UTF-8" http-equiv="Content-Type"/><title>{t}</title>'
             '<link rel="stylesheet" type="text/css" href="style.css"/></head><body><h1>{t}</h1><p>{x}</p>{extra}</body></html>')
    table = '<table summary="data"><tr><td>1</td></tr></table>'
    opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
  <dc:title>Old Book</dc:title><dc:creator opf:role="aut" opf:file-as="Writer, A.">A. Writer</dc:creator>
  <dc:language>{lang}</dc:language><dc:identifier id="bookid">urn:uuid:12345678-1234-1234-1234-123456789abc</dc:identifier>
  <meta name="cover" content="cover-img"/>
 </metadata>
 <manifest>
  <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  <item id="css" href="style.css" media-type="text/css"/>
  <item id="cover-img" href="cover.png" media-type="image/png"/>
  <item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>
  <item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/>
  <item id="c3" href="c3.xhtml" media-type="application/xhtml+xml"/>
 </manifest>
 <spine toc="ncx"><itemref idref="c1"/><itemref idref="c2"/><itemref idref="c3"/></spine>
</package>'''
    ncx = '''<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="urn:uuid:12345678-1234-1234-1234-123456789abc"/></head>
<docTitle><text>Old Book</text></docTitle><navMap>
<navPoint id="n1" playOrder="1"><navLabel><text>One</text></navLabel><content src="c1.xhtml"/></navPoint>
<navPoint id="n2" playOrder="2"><navLabel><text>Two</text></navLabel><content src="c2.xhtml"/>
 <navPoint id="n3" playOrder="3"><navLabel><text>Three</text></navLabel><content src="c3.xhtml"/></navPoint></navPoint>
</navMap></ncx>'''
    return _zip({
        "OEBPS/content.opf": opf, "OEBPS/toc.ncx": ncx, "OEBPS/style.css": "p { margin: 0 }", "OEBPS/cover.png": PNG,
        "OEBPS/c1.xhtml": xhtml.format(t="One", x=text, extra=table),
        "OEBPS/c2.xhtml": xhtml.format(t="Two", x=text, extra=""),
        "OEBPS/c3.xhtml": xhtml.format(t="Three", x=text, extra=""),
    })


def epub3_hebrew(lang="en") -> bytes:
    """An EPUB 3 Hebrew book tagged with the wrong language and without RTL settings or extras."""
    page = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">'
            '<head><title>{t}</title></head><body><h1>{t}</h1><p>{x}</p></body></html>')
    opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>ספר</dc:title><dc:language>{lang}</dc:language><dc:identifier id="id">urn:uuid:12345678-1234-1234-1234-123456789abd</dc:identifier>
  <meta property="dcterms:modified">2020-01-01T00:00:00Z</meta>
 </metadata>
 <manifest>
  <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
  <item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>
  <item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/>
 </manifest>
 <spine><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>'''
    nav = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml" '
           'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>nav</title></head><body>'
           '<nav epub:type="toc"><ol><li><a href="c1.xhtml">פרק א</a></li><li><a href="c2.xhtml">פרק ב</a></li></ol></nav></body></html>')
    return _zip({
        "OEBPS/content.opf": opf, "OEBPS/nav.xhtml": nav,
        "OEBPS/c1.xhtml": page.format(t="פרק א", x=HEB), "OEBPS/c2.xhtml": page.format(t="פרק ב", x=HEB),
    })


def _valid(data: bytes, tmp_path: Path, name: str) -> None:
    if epubcheck_available():
        path = tmp_path / name
        path.write_bytes(data)
        result = check_epub(path)
        assert result is not None and result.ok, result.errors


def test_inspect_reports_gaps():
    info = inspect_epub(epub3_hebrew())
    assert info.version == "3.0" and info.language == "en" and info.detected_language == "he"
    assert info.toc_entries == 2 and not info.has_contents_page and not info.has_accessibility
    assert any("Fix the language" in s for s in info.suggestions)
    assert any("right-to-left" in s for s in info.suggestions)


def test_enrich_hebrew_epub3(tmp_path: Path):
    _valid(epub3_hebrew(), tmp_path, "before.epub")
    data, changes = enrich_epub(epub3_hebrew())
    pkg = EpubPackage(data)
    assert pkg.dc("language")[0].text == "he"
    assert pkg.spine.get("page-progression-direction") == "rtl"
    assert pkg.meta_name("primary-writing-mode").get("content") == "horizontal-rl"
    assert pkg.meta_property("schema:accessMode")
    chapter = pkg.text("OEBPS/c1.xhtml")
    assert 'dir="rtl"' in chapter and 'lang="he"' in chapter
    spine = [r.get("idref") for r in pkg.spine]
    assert spine[0].startswith("pdf2epub-contents")
    contents = pkg.text(next(p for p in pkg.files if p.endswith("pdf2epub-contents.xhtml")))
    assert "תוכן העניינים" in contents and 'href="c2.xhtml"' in contents
    assert 'epub:type="landmarks"' in pkg.text("OEBPS/nav.xhtml")
    assert any("Language: en → he" in c for c in changes)
    _valid(data, tmp_path, "after.epub")


def test_enrich_upgrades_epub2(tmp_path: Path):
    data, changes = enrich_epub(epub2())
    pkg = EpubPackage(data)
    assert pkg.version == "3.0" and pkg.nav_item() is not None
    assert "cover-image" in pkg.properties(pkg.item("cover-img"))
    assert "<!DOCTYPE html>" in pkg.text("OEBPS/c1.xhtml") and "summary=" not in pkg.text("OEBPS/c1.xhtml")
    nav = pkg.text(pkg.item_path(pkg.nav_item()))
    assert "Three" in nav and nav.index("Two") < nav.index("<ol>", nav.index("Two"))  # nested entry kept
    assert pkg.meta_property("dcterms:modified")
    assert any("EPUB 3" in c for c in changes)
    _valid(data, tmp_path, "upgraded.epub")


def test_enrich_epub2_without_upgrade_stays_valid(tmp_path: Path):
    data, _ = enrich_epub(epub2(), EnrichOptions(upgrade=False))
    pkg = EpubPackage(data)
    assert pkg.version == "2.0" and pkg.nav_item() is None
    guide = pkg.opf.find("{http://www.idpf.org/2007/opf}guide")
    assert any(r.get("type") == "toc" for r in guide)
    _valid(data, tmp_path, "epub2.epub")


def test_enrich_is_idempotent_and_keeps_content():
    once, _ = enrich_epub(epub3_hebrew())
    twice, changes = enrich_epub(once)
    assert changes == []
    assert EpubPackage(once).text("OEBPS/c2.xhtml") == EpubPackage(twice).text("OEBPS/c2.xhtml")


def test_enrich_title_authors_cover():
    data, changes = enrich_epub(epub3_hebrew("he"), EnrichOptions(title="שם חדש", authors=["כותב"], cover_image=PNG))
    pkg = EpubPackage(data)
    assert pkg.dc("title")[0].text == "שם חדש" and [c.text for c in pkg.dc("creator")] == ["כותב"]
    cover = pkg.cover_item()
    assert cover is not None and "cover-image" in pkg.properties(cover) and pkg.files[pkg.item_path(cover)] == PNG


def test_merge_group_and_split_edits():
    def chapter(title, page, n):
        blocks = [Block(Kind.HEADING, text=title, page=page, first_on_page=True)]
        blocks += [Block(Kind.PARAGRAPH, text=f"{title} paragraph {i}.", page=page) for i in range(n)]
        return Chapter(title=title, blocks=blocks, in_toc=True)

    note = Block(Kind.FOOTNOTE, text="note", page=1, note_id="note-1", marker="*", linked=True)
    first = chapter("One", 1, 4)
    first.blocks[3].text += f"{NOTE_OPEN}note-1{NOTE_CLOSE}"
    first.notes = [note]
    book = Book(title="B", authors=[], language="en", direction="ltr",
                chapters=[first, chapter("Two", 2, 2), chapter("Three", 3, 2), chapter("Four", 4, 2)])
    edits = Edits(chapters=[
        ChapterEdit(index=0, splits=[{"at": 3, "title": "One B"}]),  # cut chapter One before its 3rd paragraph
        ChapterEdit(index=1, title="Two to Four"),
        ChapterEdit(index=2, merge=True),
        ChapterEdit(index=3, merge=True),
    ])
    out = apply_edits(book, edits)
    assert [c.title for c in out.chapters] == ["One", "One B", "Two to Four"]
    assert out.chapters[1].blocks[0].text == "One B" and out.chapters[1].notes == [out.chapters[1].notes[0]]
    assert out.chapters[1].notes[0].note_id == "note-1" and not out.chapters[0].notes  # note moved with its reference
    texts = [b.text for b in out.chapters[2].blocks]
    assert texts[0] == "Two to Four" and "Three paragraph 1." in texts and "Four paragraph 1." in texts
