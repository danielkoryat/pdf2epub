# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#     "faster-whisper>=1.1",
# ]
# ///
"""The listening check: ivrit.ai's Hebrew Whisper writes down what a voice said, and the page compares it with
the text it was given. It runs on the processor, so it never competes with the voices for the GPU's memory."""

from __future__ import annotations

import difflib
import re

from common import say, serve

MODEL = "ivrit-ai/whisper-large-v3-turbo-ct2"
_NIQQUD = re.compile("[֑-ׇ]")
_model = None


def words(text: str) -> list[str]:
    text = _NIQQUD.sub("", text).replace("־", " ").replace("-", " ")
    return re.findall(r"[\wא-ת%]+", text.lower())


def check(req: dict) -> dict:
    """What Whisper heard, word by word against the text: [word, "ok" | "wrong" | "extra"], and missed words."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        say("Loading the speech recognizer")
        _model = WhisperModel(MODEL, device="cpu", compute_type="int8")
    say("Listening")
    segments, _ = _model.transcribe(req["audio"], language="he", beam_size=5, vad_filter=False)
    heard = " ".join(s.text.strip() for s in segments)
    want, got = words(req["text"]), words(heard)
    marked, missed = [], []
    for op, a, b, c, d in difflib.SequenceMatcher(a=want, b=got, autojunk=False).get_opcodes():
        if op == "equal":
            marked += [[w, "ok"] for w in got[c:d]]
        elif op == "insert":
            marked += [[w, "extra"] for w in got[c:d]]
        elif op == "delete":
            missed += want[a:b]
            marked += [["…", "missing"]]
        else:
            marked += [[w, "wrong"] for w in got[c:d]]
            missed += want[a:b]
    right = sum(1 for _, m in marked if m == "ok")
    say("ready")
    return {"heard": heard, "marked": marked, "missed": missed, "accuracy": right / max(len(want), 1)}


if __name__ == "__main__":
    serve({}, {"check": check})
