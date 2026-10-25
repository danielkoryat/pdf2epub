"""BlueTTS (github.com/maxmelichov/BlueTTS): fast open-source Hebrew and English voices, on the processor, or on
an NVIDIA GPU when the GPU build of ONNX Runtime is installed (gpu.py).

Models come from Hugging Face at a fixed revision; the named voices (noa, lily, daniel, adam) are small style
files in the BlueTTS repository, fetched at the commit the package is installed from.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import httpx
import numpy as np

from ..audio import resample
from ..numbers import JOIN, VERSION, prepare_numbers, repair_words
from ..pronunciation import Pronunciations, corrected_processor
from ..text import PAUSE_CLAUSE, PAUSE_SENTENCE, groups, strip_niqqud, tag_latin

log = logging.getLogger(__name__)

_JOINED = re.compile(r"([ובכלמשה]{1,3})" + JOIN + r"\s*(【?)")

MODEL_REPO = "notmax123/BlueTTS2.5-onnx"
MODEL_REVISION = "468da64b4a51795a7594a3637727dbaf876b6df2"
REPO_COMMIT = "0e38dbf08ed53f85863d1eab092bd9572c53a503"  # keep in step with pyproject.toml
STEPS = 32  # refinement steps: more is smoother and slower
# Text goes to BlueTTS in sentence groups of at most this many letters; a longer sentence is cut at commas, then at
# spaces. BlueTTS splits only at . ! and ?, and its model says at most about 15 seconds at once: 100 letters took
# 7.5 s, 150 took 10.7 s, but 300 were squeezed into 15 s and 400 into 15.7 s, rushed and with whole words lost
# (Whisper heard "יצאה ילדים" for "יצאו שלושה ילדים"). 160 letters are about 11 s at normal speed. Its model also
# takes at most 1,000 sounds and its Hebrew letters-to-sounds model at most 2,046 letters; Sapiens has sentences of
# 1,800 letters without a full stop.
PIECE_CHARS = 160


class BlueEngine:
    name = "blue"
    group_chars = 0

    def __init__(self, models: Path):
        from blue_onnx import load_text_to_speech
        from huggingface_hub import snapshot_download

        self.dir = models / "blue"
        onnx_dir = self.dir / "onnx"
        # Checked once: the download takes locks inside the folder, which is read-only in the server image.
        complete = onnx_dir / f".complete-{MODEL_REVISION}"
        if not complete.is_file():
            snapshot_download(MODEL_REPO, revision=MODEL_REVISION, local_dir=onnx_dir)
            complete.touch()
        self.tts = _load_on_gpu(str(onnx_dir)) or load_text_to_speech(str(onnx_dir))
        self.pronunciations = Pronunciations.load()
        self.tts.g2p = corrected_processor(self.tts.g2p, self.pronunciations)
        self.cache_key = VERSION + ":" + self.pronunciations.fingerprint
        self.rate = self.tts.sample_rate
        self._styles: dict[str, object] = {}

    def style(self, voice: str):
        if voice not in self._styles:
            from blue_onnx import load_voice_style

            path = self.dir / "voices" / f"{voice}.json"
            if not path.is_file():
                url = f"https://raw.githubusercontent.com/maxmelichov/BlueTTS/{REPO_COMMIT}/voices/{voice}.json"
                res = httpx.get(url, timeout=60, follow_redirects=True)
                res.raise_for_status()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(res.content)
            self._styles[voice] = load_voice_style([str(path)])
        return self._styles[voice]

    def synthesize(self, text: str, lang: str, voice: str, speed: float, quality: str) -> np.ndarray:
        from blue_onnx import limit_peak

        if lang == "he":  # English goes through espeak, which reads numbers well (and years as years)
            text = spell_out(strip_niqqud(text))  # before cutting: spelled-out numbers take room
        parts: list[np.ndarray] = []
        for piece in groups(text, PIECE_CHARS):
            if piece.count("【") != piece.count("】"):  # cut inside a number marked to be read slowly
                from blue_onnx.text_norm import strip_slow_markers

                piece = strip_slow_markers(piece).replace("【", "").replace("】", "")
            if parts:
                pause = PAUSE_SENTENCE if previous.rstrip("\"'”’)]").endswith((".", "!", "?", "…", ":", ";")) else PAUSE_CLAUSE
                parts.append(np.zeros(int(pause * self.rate), dtype=np.float32))
            previous = piece
            audio, _ = self.tts(tag_latin(piece) if lang == "he" else piece, lang=lang, style=self.style(voice), total_step=STEPS,
                                speed=speed, cfg_scale=4.0, silence_duration=PAUSE_SENTENCE)
            audio = np.asarray(audio, dtype=np.float32)
            parts.append(audio[0] if audio.ndim == 2 else audio)
        return resample(limit_peak(np.concatenate(parts)), self.rate)


def spell_out(text: str) -> str:
    """Hebrew numbers in words that agree with what they count, keeping their prefixes (כ-70,000 is כשבעים אלף),
    and units, abbreviations and OCR hyphens as the narrator says them, then BlueTTS's own normalizer for dates,
    times, codes and the remaining numbers."""
    from blue_onnx.text_norm import prepare_text_for_synthesis

    text = prepare_text_for_synthesis(prepare_numbers(repair_words(text)), lang="he")
    return _JOINED.sub(r"\2\1", text).replace(JOIN, "")


def _load_on_gpu(onnx_dir: str):
    """BlueTTS with its models on the GPU (see gpu.py), or None to run it on the processor. BlueTTS's own GPU
    switch is not finished, so this builds the model the way its load_text_to_speech does, with the GPU."""
    import blue_onnx as b
    import onnxruntime as ort

    from ..gpu import providers

    gpu = providers()
    if gpu is None:
        return None
    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.inter_op_num_threads = 1
    sessions = b.load_onnx_all(onnx_dir, opts, gpu)
    if "CUDAExecutionProvider" not in sessions[2].get_providers():  # the CUDA libraries did not load
        log.warning("BlueTTS could not use the GPU; it runs on the processor")
        return None
    log.info("BlueTTS runs on the GPU")
    u_text, u_ref = b.load_uncond(onnx_dir)
    return b.TextToSpeech(b.load_cfgs(onnx_dir), b.load_text_processor(onnx_dir), *sessions, g2p=b.TextProcessor(None),
                          u_text=u_text, u_ref=u_ref, latent_stats=b.load_latent_stats(onnx_dir))
