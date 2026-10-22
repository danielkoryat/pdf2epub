"""Preparing text for the voices: sentences and chunks, Hebrew vowel marks, English inside Hebrew, pauses."""

from __future__ import annotations

import re

# Silences (seconds) that give the reading its rhythm.
PAUSE_SENTENCE = 0.35
PAUSE_PARAGRAPH = 0.7
PAUSE_HEADING = 1.0  # after a chapter title or a heading
PAUSE_BREAK = 1.5  # a scene break (* * *)
PAUSE_CHAPTER = 2.0  # at the end of a chapter

# Letters spoken per second at normal speed, to spot a voice that rambles or stops short.
CHARS_PER_SECOND = {"he": 14.0, "en": 16.0}

_NIQQUD = re.compile("[\u0591-\u05bd\u05bf\u05c1\u05c2\u05c4\u05c5\u05c7]")
_LATIN_RUN = re.compile(r"[A-Za-z](?:[A-Za-z0-9'\u2019 \-]*[A-Za-z0-9])?")
_SENTENCE_END = re.compile(r"([.!?\u2026:;\u05c3][\"'\u201d\u2019)\]]*)\s+")  # the end mark and closing quotes


def strip_niqqud(text: str) -> str:
    """Hebrew without vowel marks and cantillation; the maqaf becomes a hyphen. The Hebrew front ends of the
    engines add their own vowels and stumble over pointed text."""
    return _NIQQUD.sub("", text).replace("\u05be", "-")


def tag_latin(text: str) -> str:
    """BlueTTS reads Latin words inside Hebrew in English only when they are wrapped in <en> tags."""
    return _LATIN_RUN.sub(lambda m: f"<en>{m.group(0)}</en>", text)


def sentences(text: str) -> list[str]:
    pieces = _SENTENCE_END.split(text)  # text, end, text, end, ..., text
    joined = [pieces[i] + (pieces[i + 1] if i + 1 < len(pieces) else "") for i in range(0, len(pieces), 2)]
    return [p.strip() for p in joined if p.strip()]


def chunks(text: str, max_len: int) -> list[str]:
    """Sentences grouped into pieces of at most max_len characters; an overlong sentence is cut at commas,
    then at spaces."""
    out: list[str] = []
    for sentence in sentences(text):
        for piece in _split_long(sentence, max_len):
            if out and len(out[-1]) + 1 + len(piece) <= max_len and not out[-1].endswith((".", "!", "?", "…")):
                out[-1] = f"{out[-1]} {piece}"
            else:
                out.append(piece)
    return out


def groups(text: str, max_len: int) -> list[str]:
    """Sentences gathered into pieces of at most max_len characters, as many as fit in each; an overlong
    sentence is cut at commas, then at spaces."""
    out: list[str] = []
    for sentence in sentences(text):
        for piece in _split_long(sentence, max_len):
            if out and len(out[-1]) + 1 + len(piece) <= max_len:
                out[-1] = f"{out[-1]} {piece}"
            else:
                out.append(piece)
    return out


def _split_long(sentence: str, max_len: int) -> list[str]:
    if len(sentence) <= max_len:
        return [sentence]
    pieces: list[str] = []
    current = ""
    for word in re.split(r"(?<=[,،])\s+|\s+", sentence):
        if current and len(current) + 1 + len(word) > max_len:
            pieces.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        pieces.append(current)
    return [p[:max_len] for p in pieces]


def expected_seconds(text: str, lang: str, speed: float = 1.0) -> float:
    return len(text) / CHARS_PER_SECOND.get(lang, 14.0) / speed


def intro(title: str, authors: list[str], lang: str) -> str:
    """What the audiobook opens with: the title and who wrote it."""
    if not authors:
        return f"{title}."
    names = ", ".join(authors)
    return f"{title}. מאת {names}." if lang == "he" else f"{title}. By {names}."


def chapter_name(title: str, number: int, lang: str) -> str:
    return title or (f"פרק {number}" if lang == "he" else f"Chapter {number}")
