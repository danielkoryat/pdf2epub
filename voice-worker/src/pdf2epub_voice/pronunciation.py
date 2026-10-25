"""Phrase-scoped Hebrew pronunciation corrections for BlueTTS.

Keep the sentence intact for G2P, then replace aligned phonemes. Never replace a
bare ambiguous word globally: use its surrounding phrase in the dictionary.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Confirmed against the pinned RenikudPlus model. These are names, not rules for
# every occurrence of רנה or סאפיינס in arbitrary contexts.
DEFAULTS = {
    "רנה דקארט": "ʁenˈe dekˈaʁt",
    "הומו סאפיינס": "hˈomo sˈapjens",
}


class Pronunciations:
    def __init__(self, entries: dict[str, str]):
        if not isinstance(entries, dict):
            raise ValueError("Pronunciations must be a JSON object mapping Hebrew phrases to IPA.")
        for phrase, ipa in entries.items():
            if not isinstance(phrase, str) or not re.fullmatch(r"[א-ת]+(?: [א-ת]+)*", phrase):
                raise ValueError(f"Pronunciation key must contain Hebrew words separated by spaces: {phrase!r}")
            if not isinstance(ipa, str) or not ipa.strip() or re.search(r"[א-ת<>【】\r\n]", ipa):
                raise ValueError(f"Pronunciation must be a nonempty IPA phrase: {phrase!r}")
            if len(phrase.split()) != len(ipa.split()):
                raise ValueError(f"Pronunciation needs one IPA word per Hebrew word: {phrase!r}")
        self.entries = dict(entries)
        choices = "|".join(re.escape(p) for p in sorted(entries, key=len, reverse=True))
        self.pattern = re.compile(r"(?<![\w֐-׿])(?:" + choices + r")(?![\w֐-׿])") if choices else None
        self.fingerprint = hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @classmethod
    def load(cls) -> Pronunciations:
        entries = dict(DEFAULTS)
        path = os.environ.get("PDF2EPUB_HEBREW_PRONUNCIATIONS")
        if path:
            custom = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            cls(custom)  # validate before merging; malformed configuration must not be silently ignored
            entries.update(custom)
        return cls(entries)

    def apply(self, text: str, phonemes: str) -> str:
        if self.pattern is None or not self.pattern.search(text):
            return phonemes
        words = list(re.finditer(r"\S+", text))
        sounds = list(re.finditer(r"\S+", phonemes))
        if len(words) != len(sounds):
            log.warning("Skipping pronunciation correction: G2P word alignment changed")
            return phonemes
        edits = []
        for match in self.pattern.finditer(text):
            first = next(i for i, w in enumerate(words) if w.start() <= match.start() < w.end())
            last = next(i for i, w in enumerate(words) if w.start() < match.end() <= w.end())
            prefix = text[words[first].start():match.start()]
            suffix = text[match.end():words[last].end()]
            # Only surrounding punctuation may lie outside the matched phrase.
            # A hyphenated Hebrew prefix must not be mistaken for punctuation.
            if re.search(r"\w", prefix + suffix):
                continue
            if not sounds[first].group().startswith(prefix) or not sounds[last].group().endswith(suffix):
                log.warning("Skipping pronunciation correction: G2P punctuation changed")
                continue
            edits.append((sounds[first].start() + len(prefix), sounds[last].end() - len(suffix), self.entries[match.group()]))
        for start, end, replacement in reversed(edits):
            phonemes = phonemes[:start] + replacement + phonemes[end:]
        return phonemes


def corrected_processor(base, corrections: Pronunciations):
    """Retain BlueTTS's mixed-language tags and number/slow-reading handling."""
    from blue_onnx import TextProcessor

    class CorrectedProcessor(TextProcessor):
        def _phonemize_segment(self, content: str, lang: str) -> str:
            result = base._phonemize_segment(content, lang)
            return corrections.apply(content.strip(), result) if lang == "he" else result

    return CorrectedProcessor()
