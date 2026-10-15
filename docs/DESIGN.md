# pdf2epub — design notes

Goal: turn PDFs — born-digital or scanned — into EPUB 3 books that look professionally made on Kindle and
other readers. Hebrew (right-to-left) and English first; nothing in the pipeline is specific to one language.

## Pipeline

```
PDF ─► analyse ─► extract ───────────────────────────► clean ─────────► structure ─► write
       per page:  Docling layout model: labels,          noise, page      chapters,    ebooklib EPUB 3
       text layer reading order, pictures, tables         numbers, joins,  parts, TOC,  (+ EPUBCheck)
       or scan?   + text from MuPDF (born-digital)        footnote links,  front matter
                    or Tesseract (scanned)                numbered chapter
                                                          breaks, OCR fixes
```

The `Book` model (`model.py`) sits between extraction and writing. `pipeline.prepare()` produces a `Draft`
(the slow part); `pipeline.build()` writes it — the web UI lets the user edit the draft in between.

## Decisions and why

**Docling for layout, not for text.** Docling's PDF parser returns born-digital Hebrew as spaced-out letters
(`ס פ ר`), its pypdfium backend reverses words, and its built-in Tesseract wrapper (fixed 3× pdfium render plus
an orientation pass per page) produced about three times more letter confusions and ran pages one at a time.
So Docling only labels regions (heading, footnote, page number, picture, table, caption) and orders them;
text comes from MuPDF lines (born-digital) or Tesseract paragraphs (scanned), each assigned to the Docling
region it overlaps most.

**Our own OCR stage** (`ocr.py`, plugged into Docling through a `StandardPdfPipeline` subclass):
* pages rendered with MuPDF at ~2× the scan's native resolution (capped at 300 dpi) — measurably fewer
  confusions than both native resolution and 3×;
* Tesseract runs in parallel threads, one process per core (≈1.4 s/page on a 14-core laptop);
* Tesseract's own paragraphs are kept: it detects indentation and keeps right-to-left word order, while
  Docling's regrouping of OCR words into its layout boxes could shuffle words between boxes;
* pages that read badly (mean confidence < 60) are retried with the script Tesseract detects on that page —
  an English copyright page inside a Hebrew book goes from 52 to 93 confidence.

**One OCR language at a time.** `heb+eng` is worse than `heb` alone on Hebrew text (common Hebrew words get
read as Latin), so the main language is detected (Tesseract OSD on sample pages) and used alone; mixed pages
are handled by the retry above.

**Scanned-page detection** is per page: little meaningful text and mostly covered by an image. Text repeated
on many pages (a watermark stamped on every page) doesn't count as meaningful, and is removed from the page
before OCR. An image page carrying only *invisible* text is a scan with someone else's OCR layer (phone
scanner apps, Acrobat); such layers can be poor — one app stores Hebrew words reversed — so it is re-OCR'd.

**OCR letter fixes** (`spell.py`): Tesseract confuses Hebrew ד/ר, ה/ח, ב/כ. A word that is (nearly) unknown in
a word-frequency list but becomes a common word by swapping such letters is fixed. Real words are never
changed (ער "awake" stays), including words with Hebrew prefix letters (וחצרות = ו + חצרות).

**Chapters** (`structure.py`, `sections.py`):
* *Book mode* when most headings open a page: only those start chapters (mid-page "headings" are usually signs,
  letters or business cards quoted in the text), and only if set larger than body text.
* Pages with nothing but a large display line, and copyright pages, are not listed in the contents.
* Novels that mark chapters only with a small centred numeral mid-page (א, ב… / 1, 2… / I, II…): breaks are
  found from the numeral, from the white space it leaves (OCR often drops lone characters), or from a page
  whose text starts low; numbers are repaired in sequence and restart after a blank page or a part title.
  Parts and their chapters form a nested table of contents.

**Kindle compatibility**: EPUB 3 with NCX, `page-progression-direction="rtl"` plus Kindle's
`primary-writing-mode` for RTL books, `dir` on paragraphs whose direction differs from the book's,
pop-up footnotes (`epub:type="noteref"` → `aside epub:type="footnote"` with a back link), cover at Kindle's
recommended size, no CSS `direction` (EPUBCheck forbids it). Every build is validated with EPUBCheck when
it is available.

## Rejected / optional

* **Surya OCR** (VLM-based, GPU): about as accurate as Tesseract on Hebrew, better on Latin words inside Hebrew
  text, but ~16 s/page and needs a llama.cpp or vLLM server. A possible "best quality" mode later.
* **Docling's built-in OCR engines**: see above.

## Ideas

Bold/italic from born-digital PDFs, print page numbers as an EPUB page-list, language detection for
Latin-script scans (French/German…), letter-confusion pairs for more languages, fixing (not just detecting)
Hebrew PDFs stored in visual order, an optional AI proofreading pass for OCR'd books.
