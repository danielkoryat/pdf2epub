"""The site's voice worker API (/api/voice), with retries: a home connection and a sleeping laptop are normal."""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from . import __version__

log = logging.getLogger(__name__)

PART_BYTES = 45 * 1024 * 1024  # under the server's 50 MB per part (and Cloudflare's 100 MB per request)
RETRY_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


class Gone(Exception):
    """The job was deleted by its owner, or is no longer this worker's: stop working on it."""


class AuthError(Exception):
    """The site refused the token."""


class Client:
    def __init__(self, url: str, token: str, name: str, transport: httpx.BaseTransport | None = None, tries: int = 6):
        headers = {"User-Agent": f"pdf2epub-voice/{__version__}", "X-Voice-Worker": name}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=httpx.Timeout(120, connect=20), transport=transport)
        self.tries = tries

    def _call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        for attempt in range(self.tries):
            try:
                res = self.http.request(method, path, **kw)
            except httpx.TransportError as exc:
                if attempt == self.tries - 1:
                    raise
                log.warning("%s %s: %s; trying again", method, path, exc)
            else:
                if res.status_code == 401:
                    raise AuthError("The site refused the voice worker token")
                if res.status_code in (404, 409):
                    raise Gone(res.text[:200])
                if res.status_code not in RETRY_STATUS:
                    res.raise_for_status()
                    return res
                if attempt == self.tries - 1:
                    res.raise_for_status()
                log.warning("%s %s: HTTP %d; trying again", method, path, res.status_code)
            time.sleep(min(5 * 2**attempt, 120))
        raise AssertionError("unreachable")

    def claim(self, engines: list[str], info: dict[str, Any]) -> dict[str, Any] | None:
        res = self._call("POST", "/api/voice/claim", json={"engines": engines, "info": info})
        return None if res.status_code == 204 else res.json()

    def script(self, job_id: str) -> dict[str, Any]:
        return self._call("GET", f"/api/voice/jobs/{job_id}/script").json()

    def cover(self, job_id: str) -> bytes:
        return self._call("GET", f"/api/voice/jobs/{job_id}/cover").content

    def progress(self, job_id: str, done: int, total: int, lines: list[str], speed: float | None = None) -> None:
        body = {"done": done, "total": total, "log": lines[-50:], **({"speed": speed} if speed else {})}
        self._call("POST", f"/api/voice/jobs/{job_id}/progress", json=body)

    def upload(self, job_id: str, path: Path, seconds: float, chapters: int) -> None:
        digest = hashlib.sha256()
        parts = 0
        with open(path, "rb") as fh:
            while data := fh.read(PART_BYTES):
                digest.update(data)
                self._call("PUT", f"/api/voice/jobs/{job_id}/parts/{parts}", content=data,
                           headers={"Content-Type": "application/octet-stream"})
                parts += 1
        self._call("POST", f"/api/voice/jobs/{job_id}/finish",
                   json={"parts": parts, "sha256": digest.hexdigest(), "seconds": seconds, "chapters": chapters})

    def fail(self, job_id: str, message: str) -> None:
        self._call("POST", f"/api/voice/jobs/{job_id}/fail", json={"message": message[:500]})
