"""Locations of external tools and model files."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# The folder that holds models/, tools/ and data/: the source checkout, or PDF2EPUB_HOME (the container image
# installs the package into a virtual environment and keeps its models in /app).
PROJECT_ROOT = Path(os.environ.get("PDF2EPUB_HOME") or Path(__file__).resolve().parents[2])

_WINDOWS_TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


def tesseract_cmd() -> str:
    """Path of the Tesseract executable (env PDF2EPUB_TESSERACT, PATH, or the default Windows install)."""
    configured = os.environ.get("PDF2EPUB_TESSERACT")
    if configured:
        return configured
    found = shutil.which("tesseract")
    if found:
        return found
    if _WINDOWS_TESSERACT.is_file():
        return str(_WINDOWS_TESSERACT)
    raise RuntimeError(
        "Tesseract is not installed. Install it (Windows: https://github.com/UB-Mannheim/tesseract/wiki) "
        "or set PDF2EPUB_TESSERACT to the tesseract executable."
    )


def tessdata_dir() -> Path:
    """Project-local tessdata folder, so language models never require changes to the system install."""
    configured = os.environ.get("PDF2EPUB_TESSDATA")
    path = Path(configured) if configured else PROJECT_ROOT / "models" / "tessdata"
    path.mkdir(parents=True, exist_ok=True)
    return path


def system_tessdata_dir() -> Path | None:
    """The tessdata folder that ships with the Tesseract install, if it can be found."""
    try:
        exe = Path(tesseract_cmd())
    except RuntimeError:
        return None
    candidates = [exe.parent / "tessdata", exe.parent.parent / "share" / "tessdata", Path("/usr/share/tessdata")]
    candidates += sorted(Path("/usr/share/tesseract-ocr").glob("*/tessdata"))
    return next((c for c in candidates if c.is_dir()), None)
