"""BlueTTS (github.com/maxmelichov/BlueTTS): fast open-source Hebrew and English voices, on the processor, or on
an NVIDIA GPU when the GPU build of ONNX Runtime is installed (gpu.py).

Models come from Hugging Face at a fixed revision; the named voices (noa, lily, daniel, adam) are small style
files in the BlueTTS repository, fetched at the commit the package is installed from.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import numpy as np

from ..audio import resample
from ..text import PAUSE_SENTENCE, strip_niqqud, tag_latin

log = logging.getLogger(__name__)

MODEL_REPO = "notmax123/BlueTTS2.5-onnx"
MODEL_REVISION = "468da64b4a51795a7594a3637727dbaf876b6df2"
REPO_COMMIT = "0e38dbf08ed53f85863d1eab092bd9572c53a503"  # keep in step with pyproject.toml
STEPS = 32  # refinement steps: more is smoother and slower


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

        if lang == "he":
            text = tag_latin(strip_niqqud(text))
        audio, _ = self.tts(text, lang=lang, style=self.style(voice), total_step=STEPS, speed=speed,
                            cfg_scale=4.0, silence_duration=PAUSE_SENTENCE)
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim == 2:
            audio = audio[0]
        return resample(limit_peak(audio), self.rate)


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
