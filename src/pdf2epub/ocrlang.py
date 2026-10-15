"""Choosing the Tesseract OCR language for scanned pages and making sure its model is available."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import urllib.request
from collections import Counter
from pathlib import Path

import pymupdf

from . import config

log = logging.getLogger(__name__)

# Tesseract OSD script name -> Tesseract language model. OSD only knows scripts, so Latin-script books
# default to English unless the user passes --ocr-lang (e.g. fra, deu, spa).
SCRIPT_TO_TESS = {
    "Hebrew": "heb",
    "Arabic": "ara",
    "Latin": "eng",
    "Cyrillic": "rus",
    "Greek": "ell",
    "Han": "chi_sim",
    "Japanese": "jpn",
    "Katakana": "jpn",
    "Hiragana": "jpn",
    "Hangul": "kor",
    "Korean": "kor",
    "Devanagari": "hin",
    "Bengali": "ben",
    "Tamil": "tam",
    "Thai": "tha",
    "Armenian": "hye",
    "Georgian": "kat",
}

# Tesseract language model -> BCP 47 language tag for the EPUB.
TESS_TO_BCP47 = {
    "heb": "he", "eng": "en", "ara": "ar", "fas": "fa", "urd": "ur", "yid": "yi", "rus": "ru", "ukr": "uk",
    "ell": "el", "chi_sim": "zh-Hans", "chi_tra": "zh-Hant", "jpn": "ja", "kor": "ko", "hin": "hi",
    "ben": "bn", "tam": "ta", "tha": "th", "hye": "hy", "kat": "ka", "fra": "fr", "deu": "de", "spa": "es",
    "ita": "it", "por": "pt", "nld": "nl", "pol": "pl", "tur": "tr", "swe": "sv", "dan": "da", "nor": "no",
    "fin": "fi", "ces": "cs", "hun": "hu", "ron": "ro", "lat": "la",
}

_BEST_URL = "https://github.com/tesseract-ocr/tessdata_best/raw/main/{}.traineddata"
_FAST_URL = "https://github.com/tesseract-ocr/tessdata/raw/main/{}.traineddata"


def ensure_tessdata(langs: list[str]) -> Path:
    """Make sure every model in `langs` (plus osd and the output configs) is in the project tessdata dir."""
    target = config.tessdata_dir()
    system = config.system_tessdata_dir()
    if not (target / "configs").is_dir():
        if system is None or not (system / "configs").is_dir():
            raise RuntimeError(f"Cannot find Tesseract's 'configs' folder to copy into {target}")
        shutil.copytree(system / "configs", target / "configs")
    for lang in {*langs, "osd"}:
        dest = target / f"{lang}.traineddata"
        if dest.is_file():
            continue
        if system is not None and (system / dest.name).is_file() and lang == "osd":
            shutil.copy2(system / dest.name, dest)
            continue
        url = (_FAST_URL if lang == "osd" else _BEST_URL).format(lang)
        log.info("Downloading Tesseract model %s", url)
        tmp = dest.with_suffix(".part")
        try:
            urllib.request.urlretrieve(url, tmp)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"Could not download the Tesseract model for '{lang}' from {url}: {exc}") from exc
        tmp.replace(dest)
    return target


def _osd_script(image_path: Path, tessdata: Path) -> tuple[str, float] | None:
    proc = subprocess.run(
        [config.tesseract_cmd(), str(image_path), "stdout", "--psm", "0", "-l", "osd", "--tessdata-dir", str(tessdata)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    script = re.search(r"Script:\s*(\S+)", proc.stdout)
    conf = re.search(r"Script confidence:\s*([\d.]+)", proc.stdout)
    if not script:
        return None
    return script.group(1), float(conf.group(1)) if conf else 0.0


def detect_ocr_language(pdf: pymupdf.Document, page_indexes: list[int], samples: int = 5) -> str:
    """Guess the OCR model for scanned pages by running Tesseract's script detection on a few of them."""
    tessdata = ensure_tessdata([])
    if not page_indexes:
        return "eng"
    # Sample from the middle of the run of scanned pages: the first pages are often the cover and title.
    step = max(len(page_indexes) // (samples + 1), 1)
    chosen = page_indexes[step::step][:samples] or page_indexes[:samples]
    votes: Counter[str] = Counter()
    with tempfile.TemporaryDirectory() as tmp:
        for index in chosen:
            path = Path(tmp) / f"p{index}.png"
            pdf[index].get_pixmap(dpi=200).save(path)
            result = _osd_script(path, tessdata)
            if result:
                votes[result[0]] += result[1]
    log.info("Script detection votes: %s", dict(votes))
    if not votes:
        return "eng"
    script = votes.most_common(1)[0][0]
    return SCRIPT_TO_TESS.get(script, "eng")
