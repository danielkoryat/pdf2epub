# Hebrew pronunciation corrections

BlueTTS now applies a small phrase dictionary after RenikudPlus predicts the
sounds of the full sentence. This retains surrounding context and lets a known
pronunciation override the prediction. The initial dictionary fixes the names
רנה דקארט and הומו סאפיינס; it is not a general solution to all Hebrew mistakes.
Other engines are unchanged.

To add book-specific corrections, create a UTF-8 JSON file mapping Hebrew phrases
to IPA, and set `PDF2EPUB_HEBREW_PRONUNCIATIONS` to its absolute path before starting
the voice worker. Restart the worker after editing the file. For example:

```json
{
  "רנה דקארט": "ʁenˈe dekˈaʁt",
  "בית ספר": "bˈet sˈefeʁ"
}
```

Custom entries override built-in entries. Use one IPA word per Hebrew word and
ordinary spaces between words. Keys currently accept unpointed Hebrew letters
and spaces. Prefer specific phrases for ambiguous words; a bare ספר correction
would also override contexts where it means a barber or counting. Attached
Hebrew prefixes require their own entries. Corrections match within each voice
chunk, not across chunk boundaries. If G2P changes word alignment or surrounding
punctuation, the correction is skipped with a warning rather than risking a
replacement in the wrong place.

Corrections affect narration only. They do not modify the EPUB or change another
engine's reading. The dictionary fingerprint participates in the audiobook work
cache key, so a new worker does not resume stale chapters after a dictionary
change. Existing completed audiobooks must be generated again.

Niqqud in the source is still stripped by the existing BlueTTS preparation step;
this dictionary uses explicit sounds instead.

## Piece length

BlueTTS says at most about 15 seconds of speech in one go. Text longer than
that is squeezed into the same 15 seconds: rushed, with whole words lost. On
one test sentence, 100 letters took 7.5 s, 150 took 10.7 s, 300 took 15.0 s and
400 took 15.7 s. Text therefore goes to BlueTTS in pieces of at most 160
letters (about 11 s). A longer sentence is cut at commas first, then at spaces
into about equal parts, never between two number words. A cut inside a
sentence gets a short pause (0.2 s) rather than a sentence pause.

## Numbers in narration

BlueTTS's own normalizer reads every number in the feminine counting form
(שלוש ילדים, שתים עשרה ימים). Before it runs, a number that counts something
is put in words that agree with the counted word. The gender comes from a
list of common counted words (units, money, time, and plurals that break the
rule such as מקומות, שבועות, נשים, פעמים), then from the plural ending: ים is
masculine and ות feminine. Ranges and choices take the noun's gender throughout
(בין חמישה עשר לעשרים יישובים, שניים או שלושה ילדים). Two before a noun uses
the construct form (שני ילדים, שתי נשים), one goes after the noun (שנה אחת),
and decimals use the counting form (שבע נקודה חמש קילומטרים). `%` becomes
אחוזים for 2 to 10 and אחוז otherwise, and ₪ becomes שקלים. A number with no
counted word after it, or a year (בשנת 1992, ב-1492), keeps BlueTTS's reading.

Prefixes stay on their number, including OCR's `כ*` and `כ"` in place of `כ-`.
Comma thousands separators are resolved before sentence punctuation, and
simple ranges are read with עד. Range order is preserved as printed.

The saved Sapiens script also contains damaged OCR values such as `0 שנה` in
the timeline and `כ407 אלף שנה` in prose. Narration does not invent missing
digits or remove suspected extra digits. Those require checking the original
page and correcting the source text.

## Abbreviations and OCR hyphens

BlueTTS drops the quote mark from abbreviations, so units would be read as
letters (ק"ג as קג). Units after a number (ק"מ, ס"מ, מ"מ, ק"ג, מ"ר, סמ"ק, קמ"ש,
ש"ח) and common abbreviations (ד"ר, לפנה"ס, לסה"נ, ארה"ב, ע"י, וכו' and a few
more) are written out, with any prefix kept (בארצות הברית). Acronyms said as
words (דנ"א, תנ"ך, מנכ"ל) are unchanged.

OCR often reads a hyphen between two words as a quote mark or a star
(לקטים"ציידים, בו"זמנית, אפרו*אסיה), and BlueTTS joins hyphenated words into
one. Both become two words. After a prefix (ש"האדם, a prefix before a
quotation) the mark is dropped and the word stays whole.

Restart the voice worker to load code changes, then regenerate the audiobook;
completed recordings do not change. The preparation version participates in
the audio cache key.

## Reproducing the check

Run `scripts/check-hebrew-pronunciation.py` with the voice-worker environment:

```powershell
voice-worker/.venv/Scripts/python.exe -X utf8 scripts/check-hebrew-pronunciation.py --models PATH_TO_WORKER_MODELS --g2p PATH_TO_RENIKUD_MODEL --edge
```

The script uses the pinned BlueTTS model and a supplied cached Renikud model. It
writes `before.wav`, `after.wav`, and `report.json` to `out/hebrew-pronunciation`.
The optional `--edge` also requests a Hila comparison from Microsoft's service.
Before and after use Noa at 0.9 speed, 32 steps, and matching random seeds.

The four targeted cases check the two corrected names in two contexts each.
Eight control sentences cover ambiguous words, numbers and mixed English.
These are phoneme regression checks, not an independent listening evaluation or
an accuracy percentage for Hebrew books. Listen to both recordings before
judging naturalness; speech recognition alone cannot reliably grade vowels in
unpointed Hebrew.
