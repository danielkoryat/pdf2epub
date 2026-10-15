"""PDF -> EPUB conversion in two steps.

prepare(): analyse, extract (OCR), clean and structure the PDF into a Draft -- the slow part.
build():   write the Draft as an EPUB, after the user optionally reviewed it (see edit.py).
convert() runs both back to back.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable

import pymupdf

from . import config
from .analyze import classify_pages, page_is_cover_like, render_cover, strip_text_from_scanned
from .cleanup import clean
from .extract import ExtractSettings, extract
from .known import outline_entries
from .lang import direction_for_language, dominant_script, language_for_script, looks_visually_reversed
from .model import Block, Book, Kind
from .ocrlang import TESS_TO_BCP47, detect_ocr_language, ensure_tessdata
from .spell import Corrector
from .structure import build_chapters

log = logging.getLogger(__name__)

Progress = Callable[[str, int, int], None]


@dataclass
class Options:
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    language: str | None = None  # BCP 47; detected from the text when not given
    ocr: str = "auto"  # auto | always | never
    ocr_lang: str | None = None  # Tesseract model, e.g. heb, eng, fra; detected when not given
    threads: int = 0  # parallel Tesseract processes; 0 = number of CPU cores minus two
    cover: bool = True
    device: str = "auto"  # auto | cuda | cpu
    pages: tuple[int, int] | None = None  # 1-based inclusive page range to convert
    dump_blocks: Path | None = None
    source_name: str | None = None  # original file name, when the PDF was saved under another name


@dataclass
class Draft:
    """A structured book that has not been written yet, plus facts about how it was extracted."""

    book: Book
    pages: int
    scanned_pages: int
    ocr_lang: str | None
    ocr_fixes: int
    seconds: float
    warnings: list[str] = field(default_factory=list)


@dataclass
class Report:
    pages: int
    scanned_pages: int
    ocr_lang: str | None
    language: str
    direction: str
    chapters: int
    toc_entries: int
    footnotes: int
    footnotes_linked: int
    images: int
    seconds: float
    warnings: list[str]


def _title_from_metadata(pdf: pymupdf.Document, pdf_path: Path, source_name: str | None = None) -> str:
    meta_title = (pdf.metadata or {}).get("title", "").strip()
    if meta_title and not re.search(r"\.(docx?|pdf|indd|tex)$|^microsoft|^untitled", meta_title, re.IGNORECASE):
        return meta_title
    return re.sub(r"[_]+", " ", Path(source_name).stem if source_name else pdf_path.stem).strip()


def convert(pdf_path: Path, out_path: Path, opts: Options, progress: Progress | None = None) -> Report:
    draft = prepare(pdf_path, opts, progress)
    return build(draft, out_path, progress)


def prepare(pdf_path: Path, opts: Options, progress: Progress | None = None) -> Draft:
    with tempfile.TemporaryDirectory(prefix="pdf2epub-") as tmp:
        return _prepare(pdf_path, opts, progress or (lambda stage, done, total: None), Path(tmp))


def _prepare(pdf_path: Path, opts: Options, progress: Progress, tmp: Path) -> Draft:
    started = time.monotonic()
    warnings: list[str] = []
    with pymupdf.open(pdf_path) as original:
        title = opts.title or _title_from_metadata(original, pdf_path, opts.source_name)
        authors = opts.authors or [a for a in [(original.metadata or {}).get("author", "").strip()] if a]
        first, last = opts.pages or (1, original.page_count)
        outline = outline_entries(original.get_toc(simple=True), first, last - first + 1)

    if opts.pages:
        first, last = opts.pages
        with pymupdf.open(pdf_path) as src, pymupdf.open() as part:
            part.insert_pdf(src, from_page=first - 1, to_page=min(last, src.page_count) - 1)
            part.save(tmp / "range.pdf")
        pdf_path = tmp / "range.pdf"

    progress("analyse", 0, 1)
    with pymupdf.open(pdf_path) as pdf:
        pages = classify_pages(pdf)
        for p in pages:
            if opts.ocr == "always" or (opts.ocr == "never" and p.kind == "scanned"):
                p.kind = "scanned" if opts.ocr == "always" else "digital"
        scanned = [p.index for p in pages if p.kind == "scanned"]
        ocr_lang = (opts.ocr_lang or detect_ocr_language(pdf, scanned)) if scanned else None
    log.info("%d pages: %d scanned, %d born-digital", len(pages), len(scanned), len(pages) - len(scanned))
    progress("analyse", 1, 1)

    settings = ExtractSettings(device=opts.device)
    if opts.threads > 0:
        settings = replace(settings, ocr_threads=opts.threads)
    if scanned:
        log.info("OCR language: %s", ocr_lang)
        tessdata = ensure_tessdata(ocr_lang.split("+"))
        settings = replace(settings, ocr_lang=ocr_lang, tessdata=str(tessdata), tesseract_cmd=config.tesseract_cmd())

    work_pdf = tmp / "work.pdf"
    strip_text_from_scanned(pdf_path, pages, work_pdf)
    cover = None
    if opts.cover and pages and page_is_cover_like(pages[0]):
        with pymupdf.open(work_pdf) as work_doc:
            cover = render_cover(work_doc, 0)
    blocks = extract(
        work_pdf,
        [p.kind for p in pages],
        settings,
        progress=lambda done, total: progress("extract", done, total),
    )

    if not blocks:
        raise RuntimeError("No text or images could be extracted from the PDF.")
    if cover is not None:
        blocks = [b for b in blocks if b.page != 1]  # page 1 *is* the cover

    fixes = 0
    if ocr_lang:
        corrector = Corrector(TESS_TO_BCP47.get(ocr_lang.split("+")[0], "en"))
        if corrector.enabled:
            for b in blocks:
                if pages[b.page - 1].kind == "scanned" and b.text:
                    b.text = corrector.fix(b.text)
            fixes = sum(corrector.fixed.values())
            if fixes:
                sample = ", ".join(f"{a}→{b}" for (a, b), _ in sorted(corrector.fixed.items(), key=lambda kv: -kv[1])[:12])
                log.info("Fixed %d OCR letter confusions: %s", fixes, sample)

    digital_text = " ".join(b.text for b in blocks if pages[b.page - 1].kind != "scanned")
    if looks_visually_reversed(digital_text):
        warnings.append(
            "Hebrew text in this PDF looks stored in visual (reversed) order; the EPUB text may read backwards."
        )

    if opts.dump_blocks:
        _dump(blocks, opts.dump_blocks)

    progress("structure", 0, 1)
    blocks = clean(blocks, len(pages), outline)
    language = opts.language or _detect_language(blocks, ocr_lang)
    chapters = build_chapters(blocks)
    progress("structure", 1, 1)

    book = Book(
        title=title,
        authors=authors,
        language=language,
        direction=direction_for_language(language),
        chapters=chapters,
        cover=cover,
    )
    for w in warnings:
        log.warning(w)
    return Draft(
        book=book,
        pages=len(pages),
        scanned_pages=len(scanned),
        ocr_lang=ocr_lang,
        ocr_fixes=fixes,
        seconds=round(time.monotonic() - started, 1),
        warnings=warnings,
    )


def build(draft: Draft, out_path: Path, progress: Progress | None = None) -> Report:
    from .writer import write_epub

    progress = progress or (lambda stage, done, total: None)
    started = time.monotonic()
    book = draft.book
    progress("write", 0, 1)
    write_epub(book, out_path)
    progress("write", 1, 1)
    notes = [n for ch in book.chapters for n in ch.notes]
    return Report(
        pages=draft.pages,
        scanned_pages=draft.scanned_pages,
        ocr_lang=draft.ocr_lang,
        language=book.language,
        direction=book.direction,
        chapters=len(book.chapters),
        toc_entries=sum(ch.in_toc for ch in book.chapters),
        footnotes=len(notes),
        footnotes_linked=sum(n.linked for n in notes),
        images=sum(b.kind is Kind.IMAGE for ch in book.chapters for b in ch.blocks),
        seconds=round(draft.seconds + time.monotonic() - started, 1),
        warnings=draft.warnings,
    )


def _detect_language(blocks: list[Block], ocr_lang: str | None) -> str:
    sample = " ".join(b.text for b in blocks[:400] if b.kind is Kind.PARAGRAPH)
    script = dominant_script(sample)
    if script == "LATIN" and ocr_lang and ocr_lang != "eng" and ocr_lang in TESS_TO_BCP47:
        return TESS_TO_BCP47[ocr_lang]
    return language_for_script(script)


def _dump(blocks: list[Block], path: Path) -> None:
    rows = []
    for b in blocks:
        row = asdict(b)
        row["image"] = f"<{len(b.image)} bytes>" if b.image else None
        rows.append(row)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
