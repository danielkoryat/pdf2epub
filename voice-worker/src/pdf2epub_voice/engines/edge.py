"""Microsoft Edge's online read-aloud voices, through the edge-tts package. Free and fast, but an unofficial
use of Microsoft's service: it may change or stop working, so requests are retried with growing waits."""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from ..audio import decode

log = logging.getLogger(__name__)

TRIES = 6


class EdgeEngine:
    name = "edge"
    group_chars = 2500  # paragraphs are sent together: far fewer requests, and Edge pauses between them itself

    def synthesize(self, text: str, lang: str, voice: str, speed: float, quality: str) -> np.ndarray:
        rate = f"{round((speed - 1) * 100):+d}%"
        for attempt in range(TRIES):
            try:
                data = asyncio.run(self._fetch(text, voice, rate))
                if data:
                    return decode(data)
                raise RuntimeError("Edge returned no audio")
            except Exception as exc:  # noqa: BLE001 -- network trouble, throttling: wait and try again
                if attempt == TRIES - 1:
                    raise RuntimeError(f"Microsoft's voice service did not answer: {exc}") from exc
                wait = 5 * 2**attempt
                log.warning("Edge voice request failed (%s); trying again in %d s", exc, wait)
                time.sleep(wait)
        raise AssertionError("unreachable")

    @staticmethod
    async def _fetch(text: str, voice: str, rate: str) -> bytes:
        import edge_tts

        out = bytearray()
        async for chunk in edge_tts.Communicate(text, voice, rate=rate).stream():
            if chunk["type"] == "audio":
                out += chunk["data"]
        return bytes(out)
