"""Corrects typical OCR letter confusions using a word-frequency list.

Tesseract confuses some letter pairs that differ by a few pixels, e.g. Hebrew ד/ר and ה/ח. A word that is
(almost) never used, but becomes a common word when such a letter is swapped, is almost certainly an OCR
error. Words that are themselves common are never changed, even when a swap would give a more common word,
because only the context can tell (ער "awake" vs. עד "until").
"""

from __future__ import annotations

import logging
import re
import urllib.request
from functools import lru_cache
from itertools import product
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

# Letter pairs Tesseract confuses, per language.
CONFUSIONS: dict[str, list[tuple[str, str]]] = {
    "he": [("ד", "ר"), ("ה", "ח"), ("ב", "כ")],
}

# Prefix letters a language attaches to words (Hebrew ו/ה/ב/ל/מ/ש/כ and their combinations). A rare word that is
# a known word plus such a prefix (וחצרות = ו + חצרות) is a real word, not an OCR error.
PREFIXES: dict[str, list[str]] = {
    "he": sorted(
        {"ו", "ה", "ב", "ל", "מ", "ש", "כ", "וה", "וב", "ול", "ומ", "וש", "וכ", "שה", "שב", "של", "שמ", "שכ",
         "מה", "כש", "לכש", "וכש", "ושה", "ושב", "ושל", "ומה", "בה", "לה", "כה", "מש", "ומש"},
        key=len,
        reverse=True,
    ),
}

# Word frequencies from OpenSubtitles (github.com/hermitdave/FrequencyWords, CC BY-SA 4.0).
_FREQ_URL = "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/{0}/{0}_full.txt"

KNOWN_MIN = 5  # a word seen at least this often is a real word and is left alone...
# ...unless it has 4+ letters and the alternative is this many times more common: the frequency list is
# itself built from (partly OCR'd) subtitles and contains common OCR errors a few dozen times.
DWARFED_RATIO = 1000
FIX_MIN = 50  # a replacement must be seen at least this often
MAX_SWAPS = 2  # at most this many confusable letters are changed in one word

# Runs of letters, including Hebrew points and the apostrophe/quote characters used inside Hebrew words
# (ג'ונתן, בע"מ), so those words are seen whole and then skipped instead of being fixed in pieces.
_QUOTES = "'\"׳״"
_WORD = re.compile(rf"[\w֑-ׇ{_QUOTES}]+")


def _freq_path(lang: str) -> Path:
    return config.PROJECT_ROOT / "models" / "wordfreq" / f"{lang}.txt"


@lru_cache(maxsize=4)
def load_frequencies(lang: str) -> dict[str, int]:
    path = _freq_path(lang)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        url = _FREQ_URL.format(lang)
        log.info("Downloading word frequencies %s", url)
        tmp = path.with_suffix(".part")
        try:
            urllib.request.urlretrieve(url, tmp)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            log.warning("Could not download word frequencies for %s (%s); skipping OCR spell fixes", lang, exc)
            return {}
        tmp.replace(path)
    freqs: dict[str, int] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            word, _, count = line.rstrip("\n").rpartition(" ")
            if word and count.isdigit():
                freqs[word] = int(count)
    return freqs


class Corrector:
    def __init__(self, lang: str, freqs: dict[str, int] | None = None):
        self.pairs = CONFUSIONS.get(lang.split("-")[0], [])
        self.prefixes = PREFIXES.get(lang.split("-")[0], [])
        self.freqs = freqs if freqs is not None else (load_frequencies(lang.split("-")[0]) if self.pairs else {})
        self.swap = {}
        for a, b in self.pairs:
            self.swap.setdefault(a, set()).add(b)
            self.swap.setdefault(b, set()).add(a)
        self.fixed: dict[tuple[str, str], int] = {}
        self._cache: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.pairs and self.freqs)

    def _known_with_prefix(self, word: str) -> bool:
        return any(
            word.startswith(p) and len(word) - len(p) >= 2 and self.freqs.get(word[len(p):], 0) >= KNOWN_MIN
            for p in self.prefixes
        )

    def _best(self, word: str) -> str:
        if word in self._cache:
            return self._cache[word]
        best = word
        own = self.freqs.get(word, 0)
        if own < KNOWN_MIN and self._known_with_prefix(word):
            own = KNOWN_MIN  # a real word with a prefix: only replace it if it is dwarfed (rule below)
        if own < KNOWN_MIN or len(word) >= 4:
            positions = [i for i, ch in enumerate(word) if ch in self.swap]
            if 0 < len(positions) <= 6:
                best_count = max(FIX_MIN - 1, own * DWARFED_RATIO if own >= KNOWN_MIN else 0)
                options = [[word[i], *sorted(self.swap[word[i]])] for i in positions]
                for combo in product(*options):
                    changes = sum(1 for i, ch in zip(positions, combo) if word[i] != ch)
                    if not changes or changes > MAX_SWAPS:
                        continue
                    chars = list(word)
                    for i, ch in zip(positions, combo):
                        chars[i] = ch
                    candidate = "".join(chars)
                    count = self.freqs.get(candidate, 0)
                    if count > best_count:
                        best, best_count = candidate, count
        self._cache[word] = best
        return best

    def fix(self, text: str) -> str:
        if not self.enabled:
            return text

        def repl(m: re.Match[str]) -> str:
            token = m.group(0)
            word = token.strip(_QUOTES)  # quotation marks around a word, as opposed to inside it
            if len(word) < 2 or not word.isalpha():
                return token  # numbers, pointed words, abbreviations: leave alone
            best = self._best(word)
            if best == word:
                return token
            self.fixed[(word, best)] = self.fixed.get((word, best), 0) + 1
            start = token.index(word)
            return token[:start] + best + token[start + len(word):]

        return _WORD.sub(repl, text)
