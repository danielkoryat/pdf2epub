"""Chatterbox Multilingual (github.com/resemble-ai/chatterbox): a larger open-source model on an NVIDIA GPU.

It copies a voice from a short recording; the named voices are copied from BlueTTS reading a fixed passage, so
"noa" sounds alike in both engines. Two quirks are handled here: Chatterbox reads Hebrew with vowel marks added
by Dicta, but builds Dicta without the model file it needs (and then silently skips the vowels), so the model is
fetched and handed to it; and like other models of its kind it can now and then ramble or stop short, so a
sentence whose length is far from what its letters need is made again with another seed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import httpx
import numpy as np

from ..audio import RATE, change_tempo, resample, silence
from ..text import PAUSE_SENTENCE, chunks, expected_seconds, strip_niqqud

log = logging.getLogger(__name__)

DICTA_URL = "https://github.com/thewh1teagle/dicta-onnx/releases/download/model-files-v1.0/dicta-1.0.int8.onnx"
REFERENCE_TEXT = {
    "he": "הרכבת יצאה מתל אביב בבוקר, ונועה ישבה ליד החלון עם ספר פתוח. מחוץ לחלון חלפו פרדסים, גשרים ושדות צהובים.",
    "en": "The train left the city early in the morning. She sat by the window with an open book, watching the fields go by.",
}
MAX_CHARS = 250
TRIES = 3


class ChatterboxEngine:
    name = "chatterbox"
    group_chars = 0

    def __init__(self, models: Path, blue: Callable[[], object]):
        import torch
        from chatterbox.models.tokenizers import tokenizer
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS
        from dicta_onnx import Dicta

        if not torch.cuda.is_available():
            raise RuntimeError("Chatterbox needs an NVIDIA GPU")
        self.torch = torch
        self.dir = models / "chatterbox"
        self.dir.mkdir(parents=True, exist_ok=True)
        dicta = self.dir / "dicta-1.0.int8.onnx"
        if not dicta.is_file():
            log.info("Downloading the Hebrew vowel model for Chatterbox (300 MB)")
            _download(DICTA_URL, dicta)
        tokenizer._dicta = Dicta(str(dicta))
        self.model = ChatterboxMultilingualTTS.from_pretrained(device="cuda")
        self.blue = blue

    def reference(self, voice: str, lang: str) -> str | None:
        if voice == "default":
            return None
        path = self.dir / "voices" / f"{voice}-{lang}.wav"
        if not path.is_file():
            import soundfile as sf

            audio = self.blue().synthesize(REFERENCE_TEXT[lang], lang, voice, 1.0, "smooth")
            path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(path, audio, RATE)
        return str(path)

    def synthesize(self, text: str, lang: str, voice: str, speed: float, quality: str) -> np.ndarray:
        if lang == "he":
            text = strip_niqqud(text)
        reference = self.reference(voice, lang)
        pieces: list[np.ndarray] = []
        for n, chunk in enumerate(chunks(text, MAX_CHARS)):
            expected = expected_seconds(chunk, lang)
            best: np.ndarray | None = None
            for attempt in range(TRIES):
                self.torch.manual_seed(1 + attempt * 7919 + n)
                with self.torch.inference_mode():
                    wav = self.model.generate(chunk, language_id=lang, audio_prompt_path=reference)
                audio = resample(wav.squeeze(0).float().cpu().numpy(), self.model.sr)
                seconds = len(audio) / RATE
                if best is None or abs(seconds - expected) < abs(len(best) / RATE - expected):
                    best = audio
                if 0.3 * expected <= seconds <= 2.5 * expected + 1.5:
                    break
                log.info("Chatterbox made %.1f s for %d letters (expected about %.1f s); trying again", seconds, len(chunk), expected)
            pieces += [best, silence(PAUSE_SENTENCE)]
        audio = np.concatenate(pieces[:-1]) if pieces else silence(0)
        return change_tempo(audio, speed)


def _download(url: str, path: Path) -> None:
    tmp = path.with_suffix(".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as res:
        res.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in res.iter_bytes(1024 * 1024):
                fh.write(chunk)
    tmp.replace(path)
