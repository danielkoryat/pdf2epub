"""Docling-based extraction of PDF pages into Book-model blocks.

Born-digital pages: Docling reads the PDF's text layer and its layout model groups it into labelled items.
Scanned pages: Docling's layout model still finds headings, footnotes, page numbers, pictures and tables,
but the text comes from the paragraphs our Tesseract stage recorded (see ocr.py); each paragraph takes the
label of the layout item it overlaps most.

Pages are processed in chunks so progress can be reported, reusing one initialised pipeline (and one copy
of the layout model on the GPU).
"""

from __future__ import annotations

import io
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .model import Block, Kind

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Chunk:
    first: int  # 1-based, inclusive
    last: int
    scanned: bool


@dataclass(frozen=True)
class ExtractSettings:
    ocr_lang: str = "eng"
    tessdata: str = ""
    tesseract_cmd: str = "tesseract"
    device: str = "auto"
    ocr_threads: int = max(1, (os.cpu_count() or 4) - 2)
    image_scale: float = 2.0


def make_chunks(kinds: list[str], size: int) -> list[Chunk]:
    """Group consecutive pages of the same kind (scanned or not) into chunks of at most `size` pages."""
    chunks: list[Chunk] = []
    start = 0
    for i in range(1, len(kinds) + 1):
        boundary = i == len(kinds) or (kinds[i] == "scanned") != (kinds[start] == "scanned") or i - start >= size
        if boundary:
            chunks.append(Chunk(start + 1, i, kinds[start] == "scanned"))
            start = i
    return chunks


class Extractor:
    def __init__(self, settings: ExtractSettings, pdf_path: Path):
        self.settings = settings
        self.pdf_path = pdf_path
        self._converters: dict[bool, object] = {}

    def _converter(self, scanned: bool):
        if scanned in self._converters:
            return self._converters[scanned]
        from docling.datamodel.accelerator_options import AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        from .ocr import PageTesseractOptions, Pdf2EpubPipeline

        s = self.settings
        batch = max(8, s.ocr_threads)
        opts = PdfPipelineOptions(
            do_ocr=scanned,
            do_table_structure=True,
            generate_picture_images=True,
            images_scale=s.image_scale,
            accelerator_options=AcceleratorOptions(device=s.device, num_threads=4),
            ocr_batch_size=batch,
            layout_batch_size=batch,
            ocr_options=PageTesseractOptions(
                lang=s.ocr_lang.split("+"),
                source_pdf=str(self.pdf_path),
                tesseract_cmd=s.tesseract_cmd,
                tessdata=s.tessdata,
                threads=s.ocr_threads,
            ),
        )
        conv = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_cls=Pdf2EpubPipeline, pipeline_options=opts)}
        )
        self._converters[scanned] = conv
        return conv

    def extract_chunk(self, chunk: Chunk) -> list[Block]:
        from docling.datamodel.base_models import ConversionStatus

        from .ocr import STORES, OcrStore

        store = STORES.setdefault(str(self.pdf_path), OcrStore())
        result = self._converter(chunk.scanned).convert(
            str(self.pdf_path), page_range=(chunk.first, chunk.last), raises_on_error=False
        )
        messages = "; ".join(e.error_message for e in result.errors)
        if result.status is ConversionStatus.FAILURE or result.document is None:
            raise RuntimeError(f"Extraction failed on pages {chunk.first}-{chunk.last}: {messages}")
        if result.errors:
            failed = sorted({e.page_no for e in result.errors if getattr(e, "page_no", None)})
            if len(failed) == chunk.last - chunk.first + 1:
                raise RuntimeError(f"Extraction failed on pages {chunk.first}-{chunk.last}: {messages}")
            log.warning("pages %s were only partially extracted: %s", failed or f"{chunk.first}-{chunk.last}", messages)
        if not chunk.scanned:
            import pymupdf

            with pymupdf.open(self.pdf_path) as pdf:
                return docling_to_blocks(result.document, pdf)
        paragraphs = {n: store.pop(n) for n in range(chunk.first, chunk.last + 1)}
        return ocr_to_blocks(result.document, paragraphs)


# ---------------------------------------------------------------------------------------------------------
# Docling items -> blocks


def _label_kinds():
    from docling_core.types.doc import DocItemLabel

    return {
        DocItemLabel.TITLE: Kind.HEADING,
        DocItemLabel.SECTION_HEADER: Kind.HEADING,
        DocItemLabel.TEXT: Kind.PARAGRAPH,
        DocItemLabel.PARAGRAPH: Kind.PARAGRAPH,
        DocItemLabel.HANDWRITTEN_TEXT: Kind.PARAGRAPH,
        DocItemLabel.REFERENCE: Kind.PARAGRAPH,
        DocItemLabel.FORMULA: Kind.PARAGRAPH,
        DocItemLabel.LIST_ITEM: Kind.LIST_ITEM,
        DocItemLabel.FOOTNOTE: Kind.FOOTNOTE,
        DocItemLabel.CAPTION: Kind.CAPTION,
        DocItemLabel.PICTURE: Kind.IMAGE,
        DocItemLabel.CHART: Kind.IMAGE,
        DocItemLabel.TABLE: Kind.TABLE,
        DocItemLabel.CODE: Kind.CODE,
    }


def _encode_image(img) -> tuple[bytes, str]:
    buf = io.BytesIO()
    if img.mode in ("RGBA", "LA", "P"):
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue(), "png"
    img.convert("RGB").save(buf, "JPEG", quality=85, optimize=True)
    return buf.getvalue(), "jpg"


def _item_box(item, doc) -> tuple[int, tuple[float, float, float, float], float]:
    """(page number, (l, t, r, b) in top-left page coordinates, page height) of a Docling item."""
    prov = item.prov[0]
    height = doc.pages[prov.page_no].size.height
    bb = prov.bbox.to_top_left_origin(height)
    return prov.page_no, (bb.l, bb.t, bb.r, bb.b), height


def _fractions(doc, page: int, box: tuple[float, float, float, float]) -> tuple[float, float, float, float] | None:
    """A box in page points as fractions of the page size."""
    size = doc.pages[page].size if page in doc.pages else None
    if not size or not size.width or not size.height:
        return None
    w, h = size.width, size.height
    return (round(box[0] / w, 4), round(box[1] / h, 4), round(box[2] / w, 4), round(box[3] / h, 4))


def _visual_block(item, doc, kind: Kind) -> Block | None:
    page, box, height = _item_box(item, doc)
    block = Block(kind=kind, page=page, top=round(box[1] / height, 4) if height else 0.0, box=_fractions(doc, page, box))
    if kind is Kind.IMAGE:
        img = item.get_image(doc)
        if img is None or min(img.size) < 24:
            return None
        block.image, block.image_ext = _encode_image(img)
        block.width, block.height = img.size
    else:
        block.html = item.export_to_html(doc, add_caption=False)
        block.text = " ".join(c.text for c in item.data.table_cells)
    return block


def _heading_level(item) -> int:
    from docling_core.types.doc import DocItemLabel

    if item.label == DocItemLabel.TITLE:
        return 1
    return max(int(getattr(item, "level", 1) or 1), 1)


def _mark_first_on_page(blocks: list[Block]) -> list[Block]:
    seen: set[int] = set()
    for b in blocks:
        if b.kind is not Kind.FOOTNOTE and b.page not in seen:
            b.first_on_page = True
            seen.add(b.page)
    return blocks


@dataclass
class _Line:
    text: str
    bbox: tuple[float, float, float, float]
    size: float


def _mupdf_lines(page) -> list[_Line]:
    """Text lines of a born-digital page as MuPDF reads them (logical order, also for right-to-left text)."""
    lines = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if spans:
                text = "".join(s["text"] for s in line["spans"]).strip()
                lines.append(_Line(text, tuple(line["bbox"]), max(s["size"] for s in spans)))
    return lines


def _assign_lines(doc, pdf, page_no: int, items: list) -> dict[int, list[_Line]]:
    """Assign each MuPDF line of a page to the Docling item it overlaps most (by id(item))."""
    boxes = [(item, _item_box(item, doc)[1]) for item in items]
    assigned: dict[int, list[_Line]] = {}
    for line in _mupdf_lines(pdf[page_no - 1]):
        best, best_overlap = None, 0.0
        for item, box in boxes:
            ov = _overlap(line.bbox, box)
            if ov > best_overlap:
                best, best_overlap = item, ov
        if best is not None and best_overlap >= 0.3 * max(_area(line.bbox), 1e-6):
            assigned.setdefault(id(best), []).append(line)
    return assigned


def docling_to_blocks(doc, pdf=None) -> list[Block]:
    """Blocks of born-digital pages in Docling's reading order.

    Docling's own PDF parser splits right-to-left words into single letters (or reverses them), so when the
    MuPDF document is given, each item's text is rebuilt from the MuPDF lines that fall inside it.
    """
    from docling_core.types.doc import ContentLayer

    kinds = _label_kinds()
    items = [
        item
        for item, _level in doc.iterate_items(included_content_layers=set(ContentLayer))
        if item.prov and getattr(item, "label", None) is not None
    ]
    lines_by_item: dict[int, list[_Line]] = {}
    if pdf is not None:
        for page_no in sorted({it.prov[0].page_no for it in items}):
            lines_by_item.update(_assign_lines(doc, pdf, page_no, [it for it in items if it.prov[0].page_no == page_no]))

    blocks: list[Block] = []
    for item in items:
        kind = kinds.get(item.label)
        if kind is None:
            continue  # page headers/footers, table-of-contents pages
        if kind in (Kind.IMAGE, Kind.TABLE):
            block = _visual_block(item, doc, kind)
            if block:
                blocks.append(block)
            continue
        lines = lines_by_item.get(id(item))
        text = " ".join(l.text for l in lines) if lines else (item.text or "").strip()
        if not text:
            continue
        page, box, height = _item_box(item, doc)
        block = Block(
            kind=kind, text=text, page=page, top=round(box[1] / height, 4) if height else 0.0, box=_fractions(doc, page, box)
        )
        if lines:
            sizes = sorted(l.size for l in lines)
            block.size = round(sizes[len(sizes) // 2], 2)
        if kind is Kind.HEADING:
            block.level = _heading_level(item)
        elif kind is Kind.LIST_ITEM:
            block.marker = (getattr(item, "marker", "") or "").strip()
            block.enumerated = bool(getattr(item, "enumerated", False))
        blocks.append(block)
    return _mark_first_on_page(blocks)


def _overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _area(a: tuple[float, float, float, float]) -> float:
    return max(a[2] - a[0], 0) * max(a[3] - a[1], 0)


_BULLET = re.compile(r"^\s*[•·▪◦‣●■□\-–—]\s+")
_NUMBERED = re.compile(r"^\s*(\(?\d{1,3}[.)]|\(?[a-zא-ת][.)])\s+")


def ocr_to_blocks(doc, paragraphs: dict[int, list]) -> list[Block]:
    """Blocks of scanned pages: Tesseract paragraphs labelled by the Docling layout item they overlap.

    Paragraphs inside a picture or table belong to it and are dropped; page headers/footers are dropped.
    Order: Docling's reading order between layout items (it handles columns and odd page layouts better
    than Tesseract's block order), Tesseract's order among the paragraphs of one item.
    """
    from docling_core.types.doc import ContentLayer, DocItemLabel

    kinds = _label_kinds()
    furniture = {DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER}
    regions: dict[int, list[tuple[int, object, tuple[float, float, float, float]]]] = {}
    # (page, sort key, block)
    entries: list[tuple[int, tuple[int, int], Block]] = []
    for order, (item, _level) in enumerate(doc.iterate_items(included_content_layers=set(ContentLayer))):
        label = getattr(item, "label", None)
        if not item.prov or (label not in kinds and label not in furniture):
            continue
        page, box, _height = _item_box(item, doc)
        regions.setdefault(page, []).append((order, item, box))
        kind = kinds.get(label)
        if kind in (Kind.IMAGE, Kind.TABLE):
            block = _visual_block(item, doc, kind)
            if block:
                entries.append((page, (order, -1), block))

    for page, paras in paragraphs.items():
        height = doc.pages[page].size.height if page in doc.pages else 0.0
        last_order = -1
        for index, para in enumerate(paras):
            best, best_order, best_overlap = None, last_order, 0.0
            for order, item, box in regions.get(page, []):
                ov = _overlap(para.bbox, box)
                if ov > best_overlap:
                    best, best_order, best_overlap = item, order, ov
            share = best_overlap / _area(para.bbox) if _area(para.bbox) else 0.0
            if best is None or share < 0.3:
                best, best_order = None, last_order  # unmatched: keep it after the previous paragraph
            last_order = best_order
            label = best.label if best is not None else DocItemLabel.TEXT
            if label in furniture:
                continue
            kind = kinds.get(label, Kind.PARAGRAPH)
            if kind in (Kind.IMAGE, Kind.TABLE):
                if share >= 0.5:
                    continue  # text that is part of a picture or table
                kind = Kind.PARAGRAPH
            top = round(para.bbox[1] / height, 4) if height else 0.0
            block = Block(kind=kind, text=para.text, page=page, top=top, size=para.size, box=_fractions(doc, page, para.bbox))
            if kind is Kind.HEADING:
                block.level = _heading_level(best)
            elif kind is Kind.LIST_ITEM:
                numbered = _NUMBERED.match(block.text)
                block.enumerated = bool(numbered)
                block.text = (_NUMBERED if numbered else _BULLET).sub("", block.text, count=1)
            entries.append((page, (best_order, index), block))

    entries.sort(key=lambda e: (e[0], e[1]))
    return _mark_first_on_page([block for _page, _key, block in entries])


def extract(
    pdf_path: Path,
    page_kinds: list[str],
    settings: ExtractSettings,
    chunk_size: int = 24,
    progress: Callable[[int, int], None] | None = None,
) -> list[Block]:
    """Extract all pages and return their blocks in reading order."""
    from .ocr import STORES

    extractor = Extractor(settings, pdf_path)
    chunks = make_chunks(page_kinds, chunk_size)
    total = len(page_kinds)
    log.info("Extracting %d pages in %d chunks (%d OCR threads)", total, len(chunks), settings.ocr_threads)
    blocks: list[Block] = []
    try:
        for chunk in chunks:
            blocks += extractor.extract_chunk(chunk)
            if progress:
                progress(chunk.last, total)
    finally:
        STORES.pop(str(pdf_path), None)
    return blocks
