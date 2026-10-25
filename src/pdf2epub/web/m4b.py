"""The title, author and cover tags of a finished audiobook, read and changed without touching its sound,
chapter marks or read-along text (mutagen rewrites only the tag box and the offsets that point past it)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from mutagen.mp4 import MP4, MP4Cover

KEEP = object()  # retag(cover=KEEP): leave the picture as it is


def cover(path: Path) -> bytes | None:
    pictures = (MP4(path).tags or {}).get("covr") or []
    return bytes(pictures[0]) if pictures else None


def retag(path: Path, title: str, authors: str, picture: bytes | None | object = KEEP) -> None:
    """New title and author tags, and a new cover (bytes), no cover (None) or the same one (KEEP). The work is
    done on a copy that then replaces the file, so a failure never leaves a broken audiobook behind."""
    tmp = path.with_suffix(".retag.m4b")
    shutil.copyfile(path, tmp)
    try:
        book = MP4(tmp)
        if book.tags is None:
            book.add_tags()
        tags = book.tags
        tags["©nam"] = tags["©alb"] = [title]
        for key in ("©ART", "aART"):
            if authors:
                tags[key] = [authors]
            else:
                tags.pop(key, None)
        if picture is None:
            tags.pop("covr", None)
        elif picture is not KEEP:
            kind = MP4Cover.FORMAT_PNG if picture[:4] == b"\x89PNG" else MP4Cover.FORMAT_JPEG
            tags["covr"] = [MP4Cover(picture, imageformat=kind)]
        book.save()
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
