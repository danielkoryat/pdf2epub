"""The voice engines. Each one turns a paragraph into mono samples at audio.RATE.

  blue        BlueTTS (open source): Hebrew and English, on the processor.
  chatterbox  Chatterbox Multilingual (open source): Hebrew and English, on an NVIDIA GPU. Its voices are copied
              from a short BlueTTS recording of the voice of the same name.
  edge        Microsoft Edge's online read-aloud voices (an unofficial service).

The voices each engine offers must match the catalog the site shows (pdf2epub/voices.py).
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

VOICES = {
    "blue": {"he": ["noa", "lily", "daniel", "adam"], "en": ["noa", "lily", "daniel", "adam"]},
    "chatterbox": {"he": ["default", "noa", "lily", "daniel", "adam"], "en": ["default", "noa", "lily", "daniel", "adam"]},
    "edge": {"he": ["he-IL-HilaNeural", "he-IL-AvriNeural"],
             "en": ["en-US-AvaNeural", "en-US-AndrewNeural", "en-GB-SoniaNeural", "en-GB-RyanNeural"]},
}


class Engine(Protocol):
    name: str
    group_chars: int  # read consecutive paragraphs together up to this many letters (0: one at a time)

    def synthesize(self, text: str, lang: str, voice: str, speed: float, quality: str) -> np.ndarray:
        """Speech for text, as float32 samples at audio.RATE."""
        ...


def installed() -> list[str]:
    """Engines whose software is installed (models are fetched when an engine is first used)."""
    names = []
    if importlib.util.find_spec("blue_onnx"):
        names.append("blue")
    if importlib.util.find_spec("chatterbox") and _cuda():
        names.append("chatterbox")
    if importlib.util.find_spec("edge_tts"):
        names.append("edge")
    return names


def _cuda() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001 -- a broken torch install means no Chatterbox
        return False


class Engines:
    """Engines are loaded on first use and kept: loading a model takes seconds to a minute."""

    def __init__(self, models: Path, names: list[str]):
        self.models = models
        self.names = names
        self._loaded: dict[str, Engine] = {}

    def get(self, name: str) -> Engine:
        if name not in self.names:
            raise RuntimeError(f"The {name} voice engine is not installed on this computer")
        if name not in self._loaded:
            log.info("Loading the %s voice engine", name)
            if name == "blue":
                from .blue import BlueEngine

                self._loaded[name] = BlueEngine(self.models)
            elif name == "chatterbox":
                from .chatterbox import ChatterboxEngine

                self._loaded[name] = ChatterboxEngine(self.models, lambda: self.get("blue"))
            else:
                from .edge import EdgeEngine

                self._loaded[name] = EdgeEngine()
        return self._loaded[name]
