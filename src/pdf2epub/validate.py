"""Optional EPUB validation with W3C EPUBCheck (needs Java; both can live in the project's tools/ folder)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config


@dataclass
class CheckResult:
    errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


def _find_java() -> str | None:
    configured = os.environ.get("PDF2EPUB_JAVA")
    if configured:
        return configured
    bundled = sorted((config.PROJECT_ROOT / "tools").glob("jdk-*/bin/java*"))
    if bundled:
        return str(bundled[-1])
    return shutil.which("java")


def _find_epubcheck() -> str | None:
    configured = os.environ.get("PDF2EPUB_EPUBCHECK")
    if configured:
        return configured
    jars = sorted((config.PROJECT_ROOT / "tools").glob("epubcheck-*/epubcheck.jar"))
    return str(jars[-1]) if jars else None


def epubcheck_available() -> bool:
    return bool(_find_java() and _find_epubcheck())


def check_epub(path: Path) -> CheckResult | None:
    """Run EPUBCheck; None when it isn't installed."""
    java, jar = _find_java(), _find_epubcheck()
    if not (java and jar):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        subprocess.run([java, "-jar", jar, str(path), "--json", str(report)], capture_output=True)
        if not report.is_file():
            return CheckResult(errors=["EPUBCheck did not produce a report"], warnings=[])
        data = json.loads(report.read_text(encoding="utf-8"))
    errors, warnings = [], []
    for msg in data.get("messages", []):
        where = msg.get("locations", [{}])[0]
        text = f"{msg.get('ID')}: {msg.get('message')} ({where.get('path', '')}:{where.get('line', '')})"
        if msg.get("severity") in ("FATAL", "ERROR"):
            errors.append(text)
        elif msg.get("severity") == "WARNING":
            warnings.append(text)
    return CheckResult(errors, warnings)
