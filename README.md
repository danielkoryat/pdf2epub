# pdf2epub

Turn PDF books, born-digital **or scanned**, into clean EPUB 3 e-books that read well on Kindle and other
e-readers. Built for Hebrew (right-to-left) and English, and works for any language Tesseract can read.

* **Scanned pages are detected automatically** and OCR'd (Tesseract, in parallel on all CPU cores), including
  scans that carry a poor OCR layer from a phone scanner app.
* **Real book structure**: chapters and parts with a nested table of contents, chapter breaks marked only by a
  numeral, front matter, pop-up footnotes, pictures and tables, cover from page 1; page numbers, running
  headers and watermarks removed; paragraphs split across pages joined again.
* **Right-to-left done properly**: logical text order, RTL page turning on Kindle, mixed Hebrew/English
  paragraphs.
* **OCR clean-up**: typical letter confusions (Hebrew ד/ר, ה/ח, ב/כ) fixed with a word-frequency check.
* **Local web UI** to upload, follow progress, review and edit chapters, preview, and download. There is also a CLI.
* Output validated with W3C **EPUBCheck**.

Everything runs locally; nothing is uploaded anywhere.

## Setup

1. **Python 3.11–3.13 and [uv](https://docs.astral.sh/uv/)**, then in this folder:
   ```
   uv sync
   ```
   On Windows and Linux this installs a CUDA build of PyTorch, so Docling's layout model uses an NVIDIA GPU
   when there is one (it falls back to the CPU otherwise).
2. **Tesseract 5**, only needed for scanned PDFs. Windows: the
   [UB Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki); macOS: `brew install tesseract`;
   Debian/Ubuntu: `apt install tesseract-ocr`. Language models are downloaded automatically into
   `models/tessdata/` the first time a language is needed; the system install is never modified.
   Set `PDF2EPUB_TESSERACT` if `tesseract` is not on your PATH.
3. **Optional: EPUBCheck** (validates the output; Send-to-Kindle rejects broken EPUBs): unzip a
   [Temurin JRE](https://adoptium.net/) and [EPUBCheck](https://github.com/w3c/epubcheck/releases) into `tools/`
   (`tools/jdk-*/` and `tools/epubcheck-*/`), or point `PDF2EPUB_JAVA` / `PDF2EPUB_EPUBCHECK` at them.

## Web UI

```
uv run pdf2epub-web            # opens http://127.0.0.1:5050
uv run pdf2epub-web --port 5060 --no-browser
```

Upload a PDF, follow the progress, then **review** the result before the EPUB is written: edit the title,
author and language, rename chapters, untick false chapters from the table of contents, merge them into the
previous chapter or remove them, and preview each chapter's text. *Edit chapters* on a finished book rebuilds
it from the same OCR result without re-reading the PDF. Jobs and files are kept in `data/jobs/`.

The server listens on this computer only (127.0.0.1). The default port is 5050 (5000 is often taken by macOS
AirPlay or Docker); ports 3000–3004 and 8000–8020 are refused so it never collides with other dev servers.

## Command line

```
uv run pdf2epub book.pdf                        # writes book.epub next to the PDF
uv run pdf2epub book.pdf -o out/book.epub --title "Title" --author "Author"
uv run pdf2epub book.pdf --pages 1-30           # quick trial on part of a book
uv run pdf2epub book.pdf --ocr-lang fra         # Latin-script scans in a language other than English
uv run pdf2epub book.pdf --ocr always           # ignore an existing (bad) text layer and OCR everything
```

Run `uv run pdf2epub --help` for all options.

## How it works

```
PDF ─► analyse ─────► extract ─────────────────────────► clean ──────► structure ──► write EPUB
       per page:      Docling layout model (GPU):         page breaks,  chapters,     ebooklib, EPUB 3:
       text layer     headings, footnotes, pictures,      footnote      TOC, front    lang/dir, RTL spine,
       or scan?       tables, page numbers, reading order links, noise, matter,       Kindle writing mode,
                      + text from MuPDF (born-digital)    OCR letter    notes         pop-up footnotes,
                        or Tesseract (scanned)            fixes                       cover, CSS
```

* **Scanned page detection** (`analyze.py`): a page with almost no text but covered by an image is a scan.
  Text repeated on most pages (watermarks) doesn't count. Detection is per page, so mixed PDFs work.
  A scan that already carries an invisible OCR layer (scanner apps such as vFlat, Acrobat) is OCR'd again:
  such layers are often poor (vFlat stores Hebrew words backwards). `--ocr never` keeps them.
* **Chapters without headings** (`sections.py`): novels that mark chapters only with a small centred numeral
  (א, ב, ג / 1, 2, 3 / I, II) are detected from the numeral, the white space around it, or a page whose text
  starts low; numbering is repaired in sequence and restarts after a blank page or part title. Parts and their
  chapters form a nested table of contents.
* **Text source**: Docling's own PDF parser breaks Hebrew words into letters, so text always comes from MuPDF
  (born-digital) or Tesseract (scanned); Docling's layout decides what each piece of text *is*.
* **OCR** (`ocr.py`): pages rendered at ~2× the scan's resolution, Tesseract run in parallel (one process per
  core), paragraphs taken from Tesseract (it uses indentation). Pages that read badly are retried with the
  script Tesseract detects on that page (e.g. an English copyright page in a Hebrew book).
* **OCR letter fixes** (`spell.py`): a non-word that becomes a common word by swapping letters Tesseract
  confuses (Hebrew ד/ר, ה/ח, ב/כ) is corrected using a word-frequency list.
* **Language & direction** (`lang.py`): detected from the Unicode script of the text; RTL books get
  `page-progression-direction="rtl"` and Kindle's `primary-writing-mode`, and paragraphs in the other
  direction get their own `dir`.

## Known limitations

* OCR is ~99% accurate on clean scans. Foreign-script words inside a page (e.g. French in a Hebrew novel) and
  low-resolution scans produce some errors.
* Chapter detection relies on headings that open a page and are printed larger than the text; unusual
  typography can add or miss a chapter. Fix those in the web UI's review step.
* Old Hebrew PDFs that store text in *visual* (reversed) order are detected and reported, not yet fixed.
* Bold/italic formatting is not carried over yet.

## Development

```
uv run pytest -m "not slow"   # unit tests (seconds)
uv run pytest                 # + end-to-end conversions of generated PDFs (~1.5 min, uses the models)
```

More background on the design decisions (and the approaches that were tried and dropped) is in
[docs/DESIGN.md](docs/DESIGN.md).

## License

[AGPL-3.0-or-later](LICENSE), required because PyMuPDF and EbookLib are AGPL-3.0.

Other components: Docling (MIT), Tesseract and its language models (Apache 2.0), word frequencies from
[FrequencyWords](https://github.com/hermitdave/FrequencyWords) (CC BY-SA 4.0, downloaded at run time).

Please only convert books you have the right to use.
