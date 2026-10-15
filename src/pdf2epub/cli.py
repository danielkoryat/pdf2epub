"""Command line: pdf2epub book.pdf [-o book.epub]"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .pipeline import Options, convert
from .validate import check_epub


def _page_range(value: str) -> tuple[int, int]:
    first, _, last = value.partition("-")
    try:
        start, end = int(first), int(last or first)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a page range like 1-30") from None
    if start < 1 or end < start:
        raise argparse.ArgumentTypeError("expected a page range like 1-30")
    return start, end


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="pdf2epub", description="Convert a PDF (born-digital or scanned) into an EPUB 3 book.")
    p.add_argument("pdf", type=Path, help="input PDF file")
    p.add_argument("-o", "--output", type=Path, help="output EPUB (default: next to the PDF)")
    p.add_argument("--title", help="book title (default: PDF metadata or file name)")
    p.add_argument("--author", action="append", default=[], help="author name; repeat for several authors")
    p.add_argument("--lang", help="book language as a BCP 47 tag, e.g. he, en, fr (default: detected)")
    p.add_argument("--ocr", choices=["auto", "always", "never"], default="auto", help="when to OCR pages (default: auto)")
    p.add_argument("--ocr-lang", help="Tesseract language model, e.g. heb, eng, fra, ara (default: detected)")
    p.add_argument("--threads", type=int, default=0, help="parallel OCR processes (default: CPU cores minus two)")
    p.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto", help="device for the layout model")
    p.add_argument("--pages", type=_page_range, help="convert only these pages, e.g. 1-30")
    p.add_argument("--no-cover", action="store_true", help="don't use the first page as the cover")
    p.add_argument("--dump-blocks", type=Path, help="write the raw extracted blocks as JSON (for debugging)")
    p.add_argument("-v", "--verbose", action="store_true", help="more logging")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("docling", "docling_core", "docling_ibm_models", "httpx", "PIL", "urllib3", "RapidOCR", "transformers"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if args.verbose else logging.WARNING)
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    if not args.pdf.is_file():
        print(f"error: {args.pdf} not found", file=sys.stderr)
        return 2
    out = args.output or args.pdf.with_suffix(".epub")
    opts = Options(
        title=args.title,
        authors=args.author,
        language=args.lang,
        ocr=args.ocr,
        ocr_lang=args.ocr_lang,
        threads=args.threads,
        cover=not args.no_cover,
        device=args.device,
        pages=args.pages,
        dump_blocks=args.dump_blocks,
    )

    def progress(stage: str, done: int, total: int) -> None:
        if stage == "extract":
            logging.getLogger("pdf2epub").info("extract: %d/%d pages", done, total)

    report = convert(args.pdf, out, opts, progress)
    print(f"\nWrote {out}")
    print(
        f"  pages: {report.pages} ({report.scanned_pages} OCR'd{', ' + report.ocr_lang if report.ocr_lang else ''})\n"
        f"  language: {report.language} ({report.direction})\n"
        f"  chapters: {report.chapters} files, {report.toc_entries} in table of contents\n"
        f"  footnotes: {report.footnotes} ({report.footnotes_linked} linked)\n"
        f"  images: {report.images}\n"
        f"  time: {report.seconds}s"
    )
    for w in report.warnings:
        print(f"  warning: {w}")

    check = check_epub(out)
    if check is None:
        print("  validation: skipped (EPUBCheck/Java not found, see README)")
    elif check.ok:
        print(f"  validation: EPUBCheck passed ({len(check.warnings)} warnings)")
    else:
        print(f"  validation: EPUBCheck found {len(check.errors)} errors:")
        for e in check.errors[:20]:
            print(f"    {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
