# pdf2epub design notes

Goal: turn PDFs, born-digital or scanned, into EPUB 3 books that look professionally made on Kindle and
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
(the slow part); `pipeline.build()` writes it. The web UI lets the user edit the draft in between.

## Decisions and why

**Docling for layout, not for text.** Docling's PDF parser returns born-digital Hebrew as spaced-out letters
(`ס פ ר`), its pypdfium backend reverses words, and its built-in Tesseract wrapper (fixed 3× pdfium render plus
an orientation pass per page) produced about three times more letter confusions and ran pages one at a time.
So Docling only labels regions (heading, footnote, page number, picture, table, caption) and orders them;
text comes from MuPDF lines (born-digital) or Tesseract paragraphs (scanned), each assigned to the Docling
region it overlaps most.

**Our own OCR stage** (`ocr.py`, plugged into Docling through a `StandardPdfPipeline` subclass):
* pages rendered with MuPDF at ~2× the scan's native resolution (capped at 300 dpi), with measurably fewer
  confusions than both native resolution and 3×;
* Tesseract runs in parallel threads, one process per core (≈1.4 s/page on a 14-core laptop);
* Tesseract's own paragraphs are kept: it detects indentation and keeps right-to-left word order, while
  Docling's regrouping of OCR words into its layout boxes could shuffle words between boxes;
* pages that read badly (mean confidence < 60) are retried with the script Tesseract detects on that page:
  an English copyright page inside a Hebrew book goes from 52 to 93 confidence.

**One OCR language at a time.** `heb+eng` is worse than `heb` alone on Hebrew text (common Hebrew words get
read as Latin), so the main language is detected (Tesseract OSD on sample pages) and used alone; mixed pages
are handled by the retry above.

**Scanned-page detection** is per page: little meaningful text and mostly covered by an image. Text repeated
on many pages (a watermark stamped on every page) doesn't count as meaningful, and is removed from the page
before OCR. An image page carrying only *invisible* text is a scan with someone else's OCR layer (phone
scanner apps, Acrobat); such layers can be poor (one app stores Hebrew words reversed), so it is re-OCR'd.

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

**Chapters the book declares come first** (`known.py`): a PDF's bookmarks give exact titles and pages; a
printed contents page ("title … number" lines near the start or end, numbers mostly rising, years excluded)
is mapped to PDF pages through the page numbers printed in the page margins, and trusted only when most of
its entries are found as lines in the text. Then the declared chapters replace the guessed ones and the printed
contents page is dropped (its page numbers mean nothing in an EPUB).

**Chapter labels** (`sections.py`): when a book labels chapters "פרק ג" / "Chapter 3" / "CHAPTER III", the
labels are the chapters; OCR garbles them ("פרקר" for פרק ד), so they are renumbered in sequence when the
readable ones agree with it. Other headings between the first and the last label stay inside their chapter.

**Running heads**: short lines repeated at the top or bottom edge of many pages (book title, author, chapter
title) are removed, whether the layout model calls them headings or text; the same words further down a
page (a real title) stay.

**Manual control**: the review step lets the user merge any run of chapters into one with a new name, and
split a chapter at any paragraph from its preview (`edit.py` applies splits, then titles, then merges).

## Enriching existing EPUBs (`enrich.py`)

Surgical, not a rebuild: the ZIP is read into memory and only the package document, the navigation document,
chapter `<html>` start tags (by regex, so the publisher's markup is untouched) and new files change. Steps:
language (missing, mismatched with the text's script, or set by the user), title/authors, RTL page progression
and Kindle writing mode, `lang`/`dir` on chapters, cover marking or a new cover, a contents page from the nav
or NCX (skipped when the book already has one), landmarks, accessibility metadata, optional OCR letter fixes.
EPUB 2 books are upgraded to EPUB 3: `opf:` attributes become refining metas, a nav document is generated from
the NCX, XHTML 1.1 doctypes become HTML5, HTML4 leftovers HTML5 rejects are fixed (table `summary`, the
`http-equiv` content type), and `svg`/`scripted` manifest properties are added. Enriching twice changes nothing.
On eight real EPUBs (Hebrew and English, EPUB 2 and 3) no enrichment added an EPUBCheck error, and one book
went from 7 errors to 3.

## Audiobooks (`narration.py`, `voices.py`, `web/voice.py`, `voice-worker/`)

**Read the EPUB, not the draft.** Both converted and uploaded books end as an EPUB, so one reader serves both,
and the user's chapter edits are already in it. Chapters follow the contents (entries pointing into the
middle of a file split it); footnotes, note marks, page numbers, tables and front matter are left out; each
paragraph carries its language, so English in a Hebrew book is read by an English voice.

**The voices are made elsewhere.** The server has no GPU, so a voice worker on another computer (the owner's
laptop) takes the jobs over HTTPS: it connects out, so no port is opened at home, and it works the same for the
public site and the local app. It authenticates with a token of its own (the API is exempt from the
same-origin check, which protects cookie sessions), sees only the text and cover, and uploads the M4B in parts
under Cloudflare's 100 MB request limit. Its progress reports are the job's heartbeat; a job whose worker
falls silent is queued again and resumed from the chapters already made (ten attempts instead of two). The
voice worker is a separate project because Chatterbox pins torch and transformers versions Docling can't share,
and BlueTTS's Hebrew front end needs Python 3.12.

**Engines** (chosen from listening tests with the same Hebrew passage): BlueTTS sounded the most natural of the
open models and runs on the processor at 2.5x to 5x real time; more refinement steps (32 instead of 5) make it
smoother. Chatterbox Multilingual needs Dicta's vowel marks for Hebrew (it silently skips them when Dicta's
model is missing) and occasionally rambles, so a sentence of implausible length is made again. Microsoft's
Edge voices are kept as a fast option. Phonikud's own voices (non-commercial license), Zonos-Hebrew and
MamreTTS (non-commercial, Linux only) and Fish-Speech S2-Pro Hebrew (4.6B parameters, research license) were
not used.

## Web UI storage

Jobs are rows in a SQLite database (`data/pdf2epub.db`, WAL mode): kind (PDF conversion or EPUB enrichment),
file name, title, authors, language, status, options, summary, report, the user's edits and recent log lines.
Large files stay in `data/jobs/<id>/`. Jobs recorded by older versions as `job.json` files are imported on
start, and drafts saved by older versions are given the fields added since.

## Rejected / optional

* **Surya OCR** (VLM-based, GPU): about as accurate as Tesseract on Hebrew, better on Latin words inside Hebrew
  text, but ~16 s/page and needs a llama.cpp or vLLM server. A possible "best quality" mode later.
* **Docling's built-in OCR engines**: see above.

## Ideas

Bold/italic from born-digital PDFs, print page numbers as an EPUB page-list, footnote pop-ups for enriched EPUBs, language detection for
Latin-script scans (French/German…), letter-confusion pairs for more languages, fixing (not just detecting)
Hebrew PDFs stored in visual order, an optional AI proofreading pass for OCR'd books.
