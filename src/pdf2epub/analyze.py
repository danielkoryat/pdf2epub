"""Per-page analysis of the source PDF: born-digital vs. scanned, cover image, pre-OCR clean-up."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pymupdf

# A page needs OCR when it has fewer meaningful text characters than this...
MIN_TEXT_CHARS = 40
# ...and images cover at least this fraction of it.
MIN_SCAN_COVERAGE = 0.5


@dataclass
class PageInfo:
    index: int  # 0-based
    kind: str  # "digital" | "scanned" | "blank"
    text_chars: int
    image_coverage: float


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip().lower()


def _image_coverage(page: pymupdf.Page) -> float:
    area = abs(page.rect)
    if not area:
        return 0.0
    covered = 0.0
    for info in page.get_image_info():
        covered += abs(pymupdf.Rect(info["bbox"]) & page.rect)
    return min(covered / area, 1.0)


def _invisible_share(page: pymupdf.Page) -> float:
    """Fraction of the page's characters drawn invisibly (text render mode 3)."""
    total = invisible = 0
    for span in page.get_texttrace():
        n = len(span["chars"])
        total += n
        if span["type"] == 3:
            invisible += n
    return invisible / total if total else 0.0


def classify_pages(pdf: pymupdf.Document) -> list[PageInfo]:
    """Decide for every page whether its text layer can be used or it must be OCR'd.

    Lines that repeat on many pages (watermarks such as a site name stamped on every page) are not
    counted as meaningful text, otherwise an image-only page with a watermark would look born-digital.
    A page image with only invisible text on top is a scan that was OCR'd before (by a scanner app or
    Acrobat); such layers are often poor -- e.g. Hebrew stored backwards -- so the page is OCR'd again.
    """
    page_lines = [[_norm(l) for l in page.get_text().splitlines() if l.strip()] for page in pdf]
    repeats = Counter(line for lines in page_lines for line in set(lines))
    threshold = max(3, int(0.3 * len(pdf)))
    infos = []
    for page, lines in zip(pdf, page_lines):
        chars = sum(len(l) for l in lines if repeats[l] < threshold)
        coverage = _image_coverage(page)
        if chars and coverage >= MIN_SCAN_COVERAGE and _invisible_share(page) >= 0.9:
            chars = 0  # an old OCR layer over a scan
        if chars >= MIN_TEXT_CHARS:
            kind = "digital"
        elif coverage >= MIN_SCAN_COVERAGE:
            kind = "scanned"
        else:
            kind = "digital" if chars else "blank"
        infos.append(PageInfo(page.number, kind, chars, round(coverage, 3)))
    return infos


def strip_text_from_scanned(src: Path, pages: list[PageInfo], dst: Path) -> None:
    """Write a copy of the PDF where scanned pages have no text layer.

    On a scanned page any existing text is either a stamp drawn over the scan (a watermark) or an old OCR
    layer we are about to replace, so removing it keeps watermarks out of the OCR and the page images.
    Images and drawings are left untouched.
    """
    doc = pymupdf.open(src)
    for info in pages:
        if info.kind != "scanned":
            continue
        page = doc[info.index]
        page.add_redact_annot(page.rect, fill=False, cross_out=False)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
    doc.save(dst, garbage=3, deflate=True)


def page_is_cover_like(info: PageInfo) -> bool:
    return info.image_coverage >= 0.8 and (info.kind == "scanned" or info.text_chars < 200)


def render_cover(pdf: pymupdf.Document, index: int = 0, target_height: int = 2560) -> bytes:
    """Render a page as a JPEG cover, sized for Kindle (recommended 1600x2560)."""
    page = pdf[index]
    zoom = target_height / page.rect.height
    # Don't upscale beyond the resolution of the largest embedded image: it only makes the file bigger.
    images = [i for i in page.get_image_info() if i["height"] and pymupdf.Rect(i["bbox"]).height]
    if images:
        largest = max(images, key=lambda i: abs(pymupdf.Rect(i["bbox"])))
        native_zoom = largest["height"] / pymupdf.Rect(largest["bbox"]).height
        zoom = max(min(zoom, native_zoom), 1.0)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("jpeg", jpg_quality=88)
