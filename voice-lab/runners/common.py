"""What every runner shares. A runner is a process in its own Python environment that keeps its models loaded.
Requests arrive one per line on standard input as JSON and answers leave one per line on standard output; the
models' own printing is sent to standard error, which the lab shows as its status line."""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path

import numpy as np

# Pieces of one reading are joined with this much silence, as the audiobooks do.
PAUSE = 0.35


def lab_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    path = Path(base) / "pdf2epub-voice" / "lab"
    path.mkdir(parents=True, exist_ok=True)
    return path


def say(message: str) -> None:
    """A status line for the page (it shows the last line a runner wrote)."""
    print(message, file=sys.stderr, flush=True)


def even_loudness(audio: np.ndarray, target_db: float = -20.0, peak: float = 0.95) -> np.ndarray:
    """Every sample at the same loudness, so no voice wins a comparison by being louder."""
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    voiced = audio[np.abs(audio) > 0.01]
    if not len(voiced):
        return audio
    gain = 10 ** (target_db / 20) / max(float(np.sqrt(np.mean(voiced.astype(np.float64) ** 2))), 1e-6)
    top = float(np.max(np.abs(audio))) * gain
    if top > peak:
        gain *= peak / top
    return (audio * gain).astype(np.float32)


def join(parts: list[np.ndarray], rate: int) -> np.ndarray:
    gap = np.zeros(int(PAUSE * rate), dtype=np.float32)
    out: list[np.ndarray] = []
    for part in parts:
        if out:
            out.append(gap)
        out.append(np.asarray(part, dtype=np.float32).reshape(-1))
    return np.concatenate(out) if out else gap


class Engine:
    """load() runs once, before the first reading; synth() returns (samples, sample rate)."""

    def load(self) -> None:
        pass

    def synth(self, req: dict) -> tuple[np.ndarray, int]:
        raise NotImplementedError


def serve(engines: dict[str, Engine], ops: dict[str, Callable[[dict], dict]] | None = None) -> None:
    answer = sys.stdout
    sys.stdout = sys.stderr  # whatever the models print must not mix with the answers
    loaded: set[str] = set()
    say("ready")
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        try:
            if req.get("op", "synth") != "synth":
                result = (ops or {})[req["op"]](req)
            else:
                import soundfile as sf

                engine = engines[req["engine"]]
                load = 0.0
                if req["engine"] not in loaded:
                    say(f"Loading {req['engine']}")
                    start = time.perf_counter()
                    engine.load()
                    load = time.perf_counter() - start
                    loaded.add(req["engine"])
                say(f"Reading with {req['engine']} ({req.get('voice')})")
                start = time.perf_counter()
                audio, rate = engine.synth(req)
                took = time.perf_counter() - start
                audio = even_loudness(audio)
                sf.write(req["out"], audio, rate, subtype="PCM_16")
                result = {"seconds": len(audio) / rate, "took": took, "load": load, "loaded": sorted(loaded)}
            reply = {"id": req.get("id"), "ok": True, **result}
        except Exception as exc:  # noqa: BLE001 -- the page shows the error; the runner keeps going
            traceback.print_exc()
            reply = {"id": req.get("id"), "ok": False, "error": f"{type(exc).__name__}: {exc}"[:1500]}
        say("ready")
        answer.write(json.dumps(reply, ensure_ascii=False) + "\n")
        answer.flush()
