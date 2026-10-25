"""Tesseract OCR stage for Docling.

Replaces Docling's own Tesseract wrapper because for our purposes it
  * renders pages through pdfium at a fixed 3x zoom, which for low-resolution scans produces noticeably more
    letter confusions (e.g. Hebrew ד/ר) than rendering at ~2x the scan's native resolution with MuPDF;
  * runs an orientation-detection pass and the OCR one page at a time.
Here pages of a batch are rendered first (MuPDF is not thread-safe) and then OCR'd in parallel threads, each
running its own Tesseract process. Pages that read badly in the book's main language are retried with the
script Tesseract detects on that page, so e.g. an English copyright page in a Hebrew book comes out right.

Besides handing word cells to Docling (which its layout model needs), the stage records Tesseract's own
paragraphs per page in `OcrStore`. Text for scanned pages is built from those paragraphs: Tesseract finds
paragraph breaks from indentation and keeps right-to-left word order, while Docling's regrouping of OCR
words into layout boxes can shuffle words between boxes. Docling's layout is then only used to label each
paragraph (heading, footnote, page number...) and for pictures and tables -- see extract.py.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import statistics
import subprocess
import tempfile
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import pymupdf
from docling.datamodel.base_models import Page
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import OcrMode, OcrOptions
from docling.models.base_ocr_model import BaseOcrModel
from docling.models.stages.ocr.tesseract_utils import tesseract_box_to_bounding_rectangle
from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
from docling_core.types.doc import BoundingBox, CoordOrigin
from docling_core.types.doc.page import TextCell

log = logging.getLogger(__name__)

# Below this mean word confidence a page is retried with the script Tesseract detects on it...
RETRY_BELOW_CONFIDENCE = 60.0
# ...provided it has enough words to be a text page (not an illustration with a few specks read as letters)
RETRY_MIN_WORDS = 15
# ...and Tesseract is reasonably sure about the script.
MIN_SCRIPT_CONFIDENCE = 2.0
MAX_DPI = 300

# Tesseract's right-to-left models keep only the end of some numbers: in Sapiens 13.5 came out as 5, 500,000 and
# 10,000 as 0, all through the book. The English model reads them whole, so a number found on a right-to-left
# page is read again with it, digits only. The lost digits are on the left of the kept ones (a number runs left
# to right), up to the neighbouring word.
RTL_TESS_LANGS = {"heb", "yid", "ara", "fas", "urd", "pus", "snd", "uig", "div", "syr"}
NUMBER_LANG = "eng"
MIN_NUMBER_CONFIDENCE = 60.0
_NUMBER_WORD = re.compile(r"(?P<pre>[(\[\"'״]*)(?P<num>\d(?:[\d.,]*\d)?)(?P<post>[.,;:!?)\]\"'״]*)")
_WELL_FORMED = re.compile(r"(?:[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d*|0)(?:\.\d+)?")
# ...and they read the hyphen between a Hebrew prefix and a number as a letter or a sign: המאה הך20, לד2500,
# ה+11, כ*70. A prefix is a run of these letters in this order; ד and ך never stand before a digit otherwise.
_MARK = re.compile(r"[|ךםןףץ]+")
_GLUED_NUMBER = re.compile(r"(?P<prefix>ו?(?:ש|כש|מש)?[כלבמ]?ה?)[ךד+*\"״](?=\d)")


@dataclass
class OcrParagraph:
    text: str
    bbox: tuple[float, float, float, float]  # l, t, r, b in page points, top-left origin
    conf: float
    size: float  # median word height in page points: a proxy for the font size


@dataclass
class OcrStore:
    """Tesseract paragraphs per 1-based page number, filled by the OCR stage during a conversion."""

    pages: dict[int, list[OcrParagraph]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def put(self, page_no: int, paragraphs: list[OcrParagraph]) -> None:
        with self.lock:
            self.pages.setdefault(page_no, []).extend(paragraphs)

    def pop(self, page_no: int) -> list[OcrParagraph]:
        with self.lock:
            return self.pages.pop(page_no, [])


# One store per source PDF path; the extractor reads it after each Docling conversion.
STORES: dict[str, OcrStore] = {}


class PageTesseractOptions(OcrOptions):
    kind: ClassVar[str] = "pdf2epub_tesseract"
    canonicalize_lang: ClassVar[bool] = False
    lang: list[str] = ["eng"]
    source_pdf: str = ""  # the PDF being converted; pages are rendered from it with MuPDF
    tesseract_cmd: str = "tesseract"
    tessdata: str = ""
    threads: int = 4
    mode: OcrMode = OcrMode.FULL_PAGE


@dataclass
class _Job:
    page: Page
    rect: BoundingBox
    image_path: Path
    zoom: float
    size: tuple[int, int]


@dataclass
class _Word:
    text: str
    conf: float
    left: float
    top: float
    width: float
    height: float
    para: tuple[int, int] = (0, 0)  # Tesseract (block_num, par_num)


def ocr_zoom(page: pymupdf.Page) -> float:
    """Render zoom for OCR: about twice the resolution of the page's scan, capped at 300 dpi."""
    native = 0.0
    for info in page.get_image_info():
        bbox = pymupdf.Rect(info["bbox"])
        if bbox.width > 0 and info.get("width"):
            native = max(native, info["width"] / bbox.width)
    if not native:
        return MAX_DPI / 72
    return min(max(native * 2, 1.5), MAX_DPI / 72)


def parse_tsv(tsv: str) -> list[_Word]:
    words = []
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        text = (row.get("text") or "").strip()
        if row.get("level") == "5" and text:
            words.append(
                _Word(
                    text,
                    float(row["conf"]),
                    float(row["left"]),
                    float(row["top"]),
                    float(row["width"]),
                    float(row["height"]),
                    (int(row["block_num"]), int(row["par_num"])),
                )
            )
    return words


def group_paragraphs(words: list[_Word], zoom: float, offset: tuple[float, float]) -> list[OcrParagraph]:
    """Join words into Tesseract's paragraphs (in Tesseract's reading order), with page-point bboxes."""
    paragraphs: list[OcrParagraph] = []
    current: list[_Word] = []

    def flush() -> None:
        if not current:
            return
        l = min(w.left for w in current) / zoom + offset[0]
        t = min(w.top for w in current) / zoom + offset[1]
        r = max(w.left + w.width for w in current) / zoom + offset[0]
        b = max(w.top + w.height for w in current) / zoom + offset[1]
        text = " ".join(w.text for w in current)
        size = statistics.median(w.height for w in current) / zoom
        paragraphs.append(OcrParagraph(text, (l, t, r, b), sum(w.conf for w in current) / len(current), round(size, 2)))
        current.clear()

    for w in words:
        if current and w.para != current[-1].para:
            flush()
        current.append(w)
    flush()
    return paragraphs


def _mean_conf(words: list[_Word]) -> float:
    return sum(w.conf for w in words) / len(words) if words else 0.0


def _is_mark(w: _Word) -> bool:
    text = re.sub(r"[\s‎‏‪-‮]", "", w.text)  # Tesseract may put spaces and direction marks in a word
    return not text or _MARK.fullmatch(text) is not None or (text in ("=", "ר", "ד") and w.conf < MIN_NUMBER_CONFIDENCE)


def unglue_number(text: str) -> str:
    """הך20 -> ה-20: the hyphen Tesseract read as a letter or a sign, between a Hebrew prefix and a number."""
    m = _GLUED_NUMBER.match(text)
    return f"{m['prefix']}-{text[m.end():]}" if m and m["prefix"] else text


def completed_number(kept: str, read: str) -> str | None:
    """The whole number in `read` (the English model's reading of the spot) when it is a well-formed number that
    ends with the digits Tesseract kept, else None. Digits picked up in front of it (a speck, the line above)
    make it malformed, and the shortest well-formed ending is taken: 2500,000 -> 500,000."""
    for start in range(len(read)):
        tail = read[start:]
        if _WELL_FORMED.fullmatch(tail) and tail.endswith(kept) and len(tail) > len(kept):
            return tail
    return None


class PageTesseractModel(BaseOcrModel):
    def __init__(self, *, enabled: bool, artifacts_path: Path | None, options: PageTesseractOptions, accelerator_options: Any):
        super().__init__(enabled=enabled, artifacts_path=artifacts_path, options=options, accelerator_options=accelerator_options)
        self.options: PageTesseractOptions = options
        self._pool = ThreadPoolExecutor(max_workers=max(1, options.threads), thread_name_prefix="tesseract")
        self._env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
        self._lock = threading.Lock()
        self.store = STORES.setdefault(options.source_pdf, OcrStore())

    # -- Tesseract calls ------------------------------------------------------------------------------------

    def _run(self, image: Path, lang: str) -> list[_Word]:
        cmd = [self.options.tesseract_cmd, str(image), "stdout", "-l", lang, "--psm", "3"]
        if self.options.tessdata:
            cmd += ["--tessdata-dir", self.options.tessdata]
        cmd.append("tsv")
        proc = subprocess.run(cmd, capture_output=True, env=self._env)
        if proc.returncode != 0:
            log.warning("tesseract failed on %s: %s", image.name, proc.stderr.decode("utf-8", "replace")[-300:])
            return []
        return parse_tsv(proc.stdout.decode("utf-8", "replace"))

    def _detect_script_lang(self, image: Path) -> str | None:
        from .ocrlang import SCRIPT_TO_TESS, ensure_tessdata

        cmd = [self.options.tesseract_cmd, str(image), "stdout", "--psm", "0", "-l", "osd"]
        if self.options.tessdata:
            cmd += ["--tessdata-dir", self.options.tessdata]
        out = subprocess.run(cmd, capture_output=True, env=self._env).stdout.decode("utf-8", "replace")
        fields = dict(line.split(":", 1) for line in out.splitlines() if ":" in line)
        script = fields.get("Script", "").strip()
        try:
            confidence = float(fields.get("Script confidence", "0"))
        except ValueError:
            confidence = 0.0
        lang = SCRIPT_TO_TESS.get(script) if confidence >= MIN_SCRIPT_CONFIDENCE else None
        if lang:
            with self._lock:
                try:
                    ensure_tessdata([lang])
                except RuntimeError as exc:
                    log.warning("%s", exc)
                    return None
        return lang

    def _ocr_job(self, job: _Job) -> list[_Word]:
        main = "+".join(self.options.lang)
        words = self._run(job.image_path, main)
        conf = _mean_conf(words)
        if conf >= RETRY_BELOW_CONFIDENCE or len(words) < RETRY_MIN_WORDS:
            return words
        detected = self._detect_script_lang(job.image_path)
        candidates = []
        if detected and detected not in self.options.lang:
            candidates += [detected, f"{main}+{detected}"]
        best_words, best_conf = words, conf
        for lang in candidates:
            alt = self._run(job.image_path, lang)
            alt_conf = _mean_conf(alt)
            if alt_conf > best_conf + 5:
                best_words, best_conf = alt, alt_conf
        if best_words is not words:
            log.info("page %d: re-OCR'd with better result (confidence %.0f -> %.0f)", job.page.page_no, conf, best_conf)
        return best_words

    def _ocr_job_with_numbers(self, job: _Job) -> list[_Word]:
        words = self._ocr_job(job)
        if RTL_TESS_LANGS.isdisjoint(self.options.lang):
            return words
        # Rules and arrows of a table read as letters (the timeline's border as |, its arrows as ך or =): a lone
        # final letter or bar is never a word, and the narration would read it.
        words = [w for w in words if not _is_mark(w)]
        for w in words:
            w.text = unglue_number(w.text)
        # Every cut-short number came out as its last digit alone (13.5 -> 5, 500,000 -> 0); longer ones are whole,
        # and reading them again only risks a mark beside them read as a digit (1800! -> 11800).
        numbers = [w for w in words if (m := _NUMBER_WORD.fullmatch(w.text)) and len(m["num"]) == 1]
        if numbers:
            fixed = self._complete_numbers(job, words, numbers)
            if fixed:
                log.info("page %d: %d numbers read whole by the %s model", job.page.page_no, fixed, NUMBER_LANG)
        return words

    def _complete_numbers(self, job: _Job, words: list[_Word], numbers: list[_Word]) -> int:
        from PIL import Image

        from .ocrlang import ensure_tessdata

        with self._lock:
            try:
                ensure_tessdata([NUMBER_LANG])
            except RuntimeError as exc:
                log.warning("%s", exc)
                return 0
        fixed = 0
        with Image.open(job.image_path) as image:
            for n, w in enumerate(numbers):
                parts = _NUMBER_WORD.fullmatch(w.text)
                h, middle = w.height, w.top + w.height / 2
                beside = [o.left + o.width for o in words if o is not w and o.left + o.width <= w.left + 2
                          and abs(o.top + o.height / 2 - middle) < h]
                left = max(max(beside, default=0.0) + 2, w.left - 8 * h, 0)
                box = (int(left), int(max(0, w.top - h / 4)), int(min(image.width, w.left + w.width + h / 3)),
                       int(min(image.height, w.top + w.height + h / 4)))
                crop = job.image_path.with_name(f"{job.image_path.stem}_n{n}.png")
                image.crop(box).save(crop)
                # Punctuation is allowed so that a mark beside the number is read as itself, not as a digit.
                cmd = [self.options.tesseract_cmd, str(crop), "stdout", "-l", NUMBER_LANG, "--psm", "7",
                       "-c", "tessedit_char_whitelist=0123456789.,!?;:()[]\"'-"]
                if self.options.tessdata:
                    cmd += ["--tessdata-dir", self.options.tessdata]
                proc = subprocess.run([*cmd, "tsv"], capture_output=True, env=self._env)
                read = parse_tsv(proc.stdout.decode("utf-8", "replace")) if proc.returncode == 0 else []
                if not read or _mean_conf(read) < MIN_NUMBER_CONFIDENCE:
                    continue
                # The run of digits that ends where the kept ones are: the rightmost.
                runs = re.findall(r"\d[\d.,]*\d|\d", "".join(r.text for r in read).replace(" ", ""))
                whole = completed_number(parts["num"], runs[-1]) if runs else None
                if whole:
                    w.text = parts["pre"] + whole + parts["post"]
                    fixed += 1
        return fixed

    # -- Docling interface ----------------------------------------------------------------------------------

    def __call__(self, conv_res: ConversionResult, page_batch: Iterable[Page]) -> Iterable[Page]:
        pages = list(page_batch)
        if not self.enabled:
            yield from pages
            return
        source = self.options.source_pdf or str(conv_res.input.file)
        with tempfile.TemporaryDirectory(prefix="pdf2epub-ocr-") as tmp, pymupdf.open(source) as pdf:
            jobs: list[_Job] = []
            for page in pages:
                if page._backend is None or not page._backend.is_valid():
                    continue
                mpage = pdf[page.page_no - 1]  # Docling pages are 1-based
                zoom = ocr_zoom(mpage)
                for n, rect in enumerate(self.get_ocr_rects(page)):
                    if rect.area() == 0:
                        continue
                    clip = pymupdf.Rect(rect.l, rect.t, rect.r, rect.b)
                    pix = mpage.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, colorspace=pymupdf.csGRAY)
                    path = Path(tmp) / f"p{page.page_no}_{n}.png"
                    pix.save(path)
                    jobs.append(_Job(page, rect, path, zoom, (pix.width, pix.height)))
            results = list(self._pool.map(self._ocr_job_with_numbers, jobs))

        cells_by_page: dict[int, list[TextCell]] = {id(p): [] for p in pages}
        for job, words in zip(jobs, results):
            self.store.put(job.page.page_no, group_paragraphs(words, job.zoom, (job.rect.l, job.rect.t)))
            cells = cells_by_page[id(job.page)]
            for w in words:
                bbox = BoundingBox(l=w.left, t=w.top, r=w.left + w.width, b=w.top + w.height, coord_origin=CoordOrigin.TOPLEFT)
                rect = tesseract_box_to_bounding_rectangle(
                    bbox, original_offset=job.rect, scale=job.zoom, orientation=0, im_size=job.size
                )
                cells.append(
                    TextCell(index=len(cells), text=w.text, orig=w.text, from_ocr=True, confidence=w.conf / 100.0, rect=rect)
                )
        for page in pages:
            if page._backend is not None and page._backend.is_valid():
                self.post_process_cells(cells_by_page[id(page)], page, conv_res)
            yield page

    @classmethod
    def get_options_type(cls) -> type[OcrOptions]:
        return PageTesseractOptions


class Pdf2EpubPipeline(StandardPdfPipeline):
    """Docling's standard PDF pipeline with our Tesseract stage plugged in."""

    def _make_ocr_model(self, art_path: Path | None) -> Any:
        options = self.pipeline_options.ocr_options
        if isinstance(options, PageTesseractOptions):
            return PageTesseractModel(
                enabled=self.pipeline_options.do_ocr,
                artifacts_path=art_path,
                options=options,
                accelerator_options=self.pipeline_options.accelerator_options,
            )
        return super()._make_ocr_model(art_path)
