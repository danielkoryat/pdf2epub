"""Checks on uploaded files, done in the web process without parsing their content.

Heavy parsing (PDF rendering, XML) only happens in the sandboxed worker. Here a file is accepted or refused from
its first bytes, its size, and, for an EPUB, the ZIP's central directory: how many files it has, how big they
unpack, whether any name tries to escape the folder or is encrypted. That stops zip bombs and path tricks before
anything is unpacked.
"""

from __future__ import annotations

import posixpath
import zipfile
from pathlib import Path

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_COMPRESSION_RATIO = 200  # a real EPUB compresses text maybe 3-10x; bombs reach thousands


class UploadRejected(ValueError):
    """The file is refused; the message is safe to show to the user."""


def sniff(first_bytes: bytes, filename: str) -> str:
    """'pdf' or 'epub' from the file's first bytes; the name only distinguishes EPUB from other ZIPs."""
    if first_bytes.lstrip(b"\r\n\t ")[:5] == PDF_MAGIC:
        return "pdf"
    if first_bytes.startswith(ZIP_MAGIC) and (b"mimetypeapplication/epub+zip" in first_bytes[:120] or filename.lower().endswith(".epub")):
        return "epub"
    raise UploadRejected("Only PDF and EPUB files can be uploaded")


def check_epub(path: Path, max_entries: int, max_unpacked_mb: int) -> None:
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
    except (zipfile.BadZipFile, OSError, ValueError):
        raise UploadRejected("The EPUB file is damaged") from None
    if len(infos) > max_entries:
        raise UploadRejected(f"The EPUB has too many files (more than {max_entries})")
    names = {i.filename for i in infos}
    if "META-INF/container.xml" not in names:
        raise UploadRejected("This is not an EPUB file (META-INF/container.xml is missing)")
    unpacked = 0
    for info in infos:
        name = info.filename
        normal = posixpath.normpath(name)
        if name.startswith(("/", "\\")) or "\\" in name or normal.startswith("..") or ":" in name.split("/")[0]:
            raise UploadRejected("The EPUB contains an unsafe file name")
        if info.flag_bits & 0x1:
            raise UploadRejected("Encrypted EPUBs (with DRM) cannot be processed")
        if info.external_attr >> 16 & 0o170000 == 0o120000:
            raise UploadRejected("The EPUB contains a symbolic link")
        unpacked += info.file_size
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO and info.file_size > 1_000_000:
            raise UploadRejected("The EPUB looks like a compression bomb")
    if unpacked > max_unpacked_mb * 1024 * 1024:
        raise UploadRejected(f"The EPUB unpacks to more than {max_unpacked_mb} MB")


def check_image(data: bytes, max_bytes: int = 20 * 1024 * 1024) -> str:
    """'image/jpeg' or 'image/png'; anything else is refused."""
    if len(data) > max_bytes:
        raise UploadRejected("The image is larger than 20 MB")
    if data.startswith(JPEG_MAGIC):
        return "image/jpeg"
    if data.startswith(PNG_MAGIC):
        return "image/png"
    raise UploadRejected("The cover must be a JPEG or PNG image")


def safe_filename(name: str) -> str:
    """A display name for an upload: no folders, no control characters, at most 200 characters."""
    base = Path(name.replace("\\", "/")).name or "book"
    clean = "".join(ch for ch in base if ch.isprintable() and ch not in '<>:"/\\|?*')
    return clean.strip()[:200] or "book"
