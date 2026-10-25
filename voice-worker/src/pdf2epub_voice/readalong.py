"""Versioned timed narration in a private ISO BMFF UUID box (ignored by ordinary players)."""
import json
import struct
from pathlib import Path

BOX_ID = bytes.fromhex("914bbd195f3b4b64a32fa44bb9cf8e11")


def embed(path: Path, cues: list[dict]) -> None:
    payload = json.dumps({"version": 1, "cues": cues}, ensure_ascii=False).encode("utf-8")
    if len(payload) > 16 * 1024 * 1024:
        raise ValueError("Read-along text exceeds 16 MB")
    with path.open("ab") as stream:
        stream.write(struct.pack(">I4s", 24 + len(payload), b"uuid") + BOX_ID + payload)
