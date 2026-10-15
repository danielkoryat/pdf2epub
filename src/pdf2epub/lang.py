"""Script, language and text-direction detection from Unicode properties (works for any language)."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

RTL_SCRIPTS = {"HEBREW", "ARABIC", "SYRIAC", "THAANA", "NKO"}

# Default BCP 47 language for a script when nothing more specific is known.
SCRIPT_TO_LANG = {
    "HEBREW": "he",
    "ARABIC": "ar",
    "CYRILLIC": "ru",
    "GREEK": "el",
    "CJK": "zh",
    "HIRAGANA": "ja",
    "KATAKANA": "ja",
    "HANGUL": "ko",
    "DEVANAGARI": "hi",
    "BENGALI": "bn",
    "TAMIL": "ta",
    "THAI": "th",
    "ARMENIAN": "hy",
    "GEORGIAN": "ka",
    "LATIN": "en",
}

RTL_LANGS = {"he", "ar", "fa", "ur", "yi", "ps", "sd", "ug", "dv", "syr"}

_SCRIPT_NAMES = sorted(SCRIPT_TO_LANG, key=len, reverse=True) + ["SYRIAC", "THAANA", "NKO"]


def char_script(ch: str) -> str | None:
    """Unicode script family of a letter, e.g. 'HEBREW' or 'LATIN'; None for non-letters."""
    if not ch.isalpha():
        return None
    name = unicodedata.name(ch, "")
    for script in _SCRIPT_NAMES:
        if name.startswith(script) or (script == "CJK" and "CJK" in name):
            return script
    return "OTHER"


def script_counts(text: str) -> Counter[str]:
    return Counter(s for s in map(char_script, text) if s)


def dominant_script(text: str) -> str | None:
    counts = script_counts(text)
    return counts.most_common(1)[0][0] if counts else None


def language_for_script(script: str | None) -> str:
    return SCRIPT_TO_LANG.get(script or "", "en")


def direction_for_language(lang: str) -> str:
    return "rtl" if lang.split("-")[0].lower() in RTL_LANGS else "ltr"


def base_direction(text: str) -> str | None:
    """Direction of the first strong character (the Unicode bidi 'first strong' rule)."""
    for ch in text:
        bidi = unicodedata.bidirectional(ch)
        if bidi == "L":
            return "ltr"
        if bidi in ("R", "AL"):
            return "rtl"
    return None


_HEBREW_FINALS = "ךםןףץ"
_HEBREW_WORD = re.compile(r"[א-ת]{2,}")


def looks_visually_reversed(text: str) -> bool:
    """True when Hebrew text appears to be stored in visual (reversed) order.

    Final letter forms (ך ם ן ף ץ) only occur at the end of a word in logical order, so finding them at
    the start of words much more often than at the end is a strong sign the characters are reversed.
    """
    at_start = at_end = 0
    for word in _HEBREW_WORD.findall(text):
        at_start += word[0] in _HEBREW_FINALS
        at_end += word[-1] in _HEBREW_FINALS
    return at_start >= 5 and at_start > 3 * at_end
