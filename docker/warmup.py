"""Runs once while the container image is built (the only time it has network access).

Downloads everything the converter would otherwise fetch on first use: the Tesseract language models, the
Hebrew word list for OCR spelling fixes, EPUBCheck, and Docling's layout and table models. Then it converts a
small generated book (one born-digital page, one scanned page) end to end, so a broken image fails the build
instead of the first user's job.

usage: python warmup.py heb,eng,ara
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import pymupdf

from pdf2epub import config
from pdf2epub.cli import main as convert
from pdf2epub.ocrlang import ensure_tessdata
from pdf2epub.spell import load_frequencies

EPUBCHECK = "5.4.0"
EPUBCHECK_SHA256 = "33350c61038e71dfb3d45a76aed04bf5481e6d5500cb780f6e98db8bbd15a28c"


def fetch_epubcheck() -> None:
    tools = config.PROJECT_ROOT / "tools"
    url = f"https://github.com/w3c/epubcheck/releases/download/v{EPUBCHECK}/epubcheck-{EPUBCHECK}.zip"
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != EPUBCHECK_SHA256:
        raise SystemExit(f"EPUBCheck download does not match its checksum ({digest})")
    tools.mkdir(parents=True, exist_ok=True)
    archive = tools / "epubcheck.zip"
    archive.write_bytes(data)
    with zipfile.ZipFile(archive) as z:
        z.extractall(tools)
    archive.unlink()


def sample_pdf(path: Path) -> None:
    doc = pymupdf.open()
    text = "Chapter One\n\nThis is a short test book. It has one page with real text and one scanned page."
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(72, 72, 540, 400), text, fontsize=14)
    # The same page again, but only as a picture: a "scanned" page that has to go through OCR.
    picture = page.get_pixmap(dpi=200)
    scanned = doc.new_page()
    scanned.insert_image(scanned.rect, pixmap=picture)
    doc.save(path)


def run(langs: list[str]) -> None:
    ensure_tessdata(langs)
    if not load_frequencies("he"):
        raise SystemExit("Could not download the Hebrew word list")
    fetch_epubcheck()
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "sample.pdf"
        sample_pdf(pdf)
        out = Path(tmp) / "sample.epub"
        code = convert([str(pdf), "-o", str(out), "--ocr-lang", "eng", "--device", "cpu"])
        if code or not out.is_file():
            raise SystemExit("The test conversion failed")
    print(f"warm-up done: models for {', '.join(langs)}, EPUBCheck {EPUBCHECK}, Docling models cached")


if __name__ == "__main__":
    run([lang for lang in sys.argv[1].split(",") if lang])
