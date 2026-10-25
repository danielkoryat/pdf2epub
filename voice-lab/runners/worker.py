"""The engines that run in the voice worker's own environment (voice-worker/.venv), using its code where it has
some: BlueTTS 2.5, BlueV3, Chatterbox, Microsoft Edge, Meta MMS and Google Gemini. It also turns Hebrew into the
stressed IPA that Qwen3-TTS Hebrew reads, with BlueTTS's front end (numbers in words, then RenikudPlus)."""

from __future__ import annotations

import base64
import io
import os
import re

from pdf2epub_voice.gpu import prefer_gpu_runtime

prefer_gpu_runtime()  # before anything imports onnxruntime

import numpy as np  # noqa: E402

from common import Engine, join, lab_dir, say, serve  # noqa: E402
from pdf2epub_voice.audio import RATE, change_tempo  # noqa: E402
from pdf2epub_voice.cli import models_dir  # noqa: E402
from pdf2epub_voice.text import chunks, groups, strip_niqqud, tag_latin  # noqa: E402

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_STYLE = "Calm, clear audiobook narration in Hebrew, at an even pace"
_blue = None
_g2p = None


def blue():
    """BlueTTS 2.5, shared with Chatterbox (its named voices are copied from BlueTTS recordings)."""
    global _blue
    if _blue is None:
        from pdf2epub_voice.engines.blue import BlueEngine

        _blue = BlueEngine(models_dir())
    return _blue


class Blue(Engine):
    def load(self):
        blue()

    def synth(self, req):
        return blue().synthesize(req["text"], "he", req["voice"], req["speed"], "smooth"), RATE


class BlueV3(Engine):
    """BlueV3 (notmax123/BlueV3-onnx) through BlueTTS 2.5's code. Two differences: its vector estimator has
    classifier-free guidance built in (so cfg_scale stays 1, or it is applied twice), and its vocoder takes the
    144-channel latent after un-normalizing, which BlueTTS's code only does for 24-channel vocoders."""

    REPO = "notmax123/BlueV3-onnx"
    STEPS = 8  # what its model card uses

    def load(self):
        import blue_onnx as b
        from huggingface_hub import snapshot_download

        from pdf2epub_voice.engines.blue import _load_on_gpu

        self.dir = lab_dir() / "models" / "bluev3"
        snapshot_download(self.REPO, local_dir=self.dir)
        tts = _load_on_gpu(str(self.dir)) or b.load_text_to_speech(str(self.dir))
        tts._prepare_vocoder_latent = lambda xt: (xt / tts._normalizer_scale) * tts._latent_std + tts._latent_mean
        self.tts = tts
        self.styles = {}

    def synth(self, req):
        import blue_onnx as b
        from blue_onnx.text_norm import strip_slow_markers

        from pdf2epub_voice.engines.blue import PIECE_CHARS, spell_out

        voice = req["voice"]
        if voice not in self.styles:
            self.styles[voice] = b.load_voice_style([str(self.dir / "voices" / f"{voice}.json")])
        parts = []
        for piece in groups(spell_out(strip_niqqud(req["text"])), PIECE_CHARS):
            if piece.count("【") != piece.count("】"):
                piece = strip_slow_markers(piece).replace("【", "").replace("】", "")
            audio, _ = self.tts(tag_latin(piece), lang="he", style=self.styles[voice], total_step=self.STEPS,
                                speed=req["speed"], cfg_scale=1.0)
            audio = np.asarray(audio, dtype=np.float32)
            parts.append(audio[0] if audio.ndim == 2 else audio)
        return b.limit_peak(join(parts, self.tts.sample_rate)), self.tts.sample_rate


class Chatterbox(Engine):
    def load(self):
        from pdf2epub_voice.engines.chatterbox import ChatterboxEngine

        self.engine = ChatterboxEngine(models_dir(), blue)

    def synth(self, req):
        return self.engine.synthesize(req["text"], "he", req["voice"], req["speed"], "smooth"), RATE


class Edge(Engine):
    def load(self):
        from pdf2epub_voice.engines.edge import EdgeEngine

        self.engine = EdgeEngine()

    def synth(self, req):
        return self.engine.synthesize(req["text"], "he", req["voice"], req["speed"], "smooth"), RATE


class Mms(Engine):
    """Meta's MMS Hebrew (facebook/mms-tts-heb): reads the letters as they are, one sentence at a time."""

    def load(self):
        import torch
        from transformers import AutoTokenizer, VitsModel

        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.tokenizer = AutoTokenizer.from_pretrained("facebook/mms-tts-heb")
        self.model = VitsModel.from_pretrained("facebook/mms-tts-heb").to(self.device)

    def synth(self, req):
        parts = []
        for n, chunk in enumerate(chunks(strip_niqqud(req["text"]), 250)):
            inputs = self.tokenizer(chunk, return_tensors="pt").to(self.device)
            self.torch.manual_seed(n)
            with self.torch.no_grad():
                parts.append(self.model(**inputs).waveform[0].float().cpu().numpy())
        rate = self.model.config.sampling_rate
        return change_tempo(join(parts, rate), req["speed"], rate), rate


class Gemini(Engine):
    """Google's Gemini TTS over its Interactions API. The key comes from the page or GEMINI_API_KEY."""

    def __init__(self, model: str):
        self.model = model

    def synth(self, req):
        import httpx
        import soundfile as sf

        key = req.get("key") or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("Gemini needs an API key: paste one into the box at the top of the page")
        body = {
            "model": self.model,
            "input": [{"type": "user_input", "content": [
                {"type": "text", "text": req["text"], "annotations": [{"type": "speech_metadata", "style": GEMINI_STYLE}]}]}],
            "response_format": {"type": "audio"},
            "generation_config": {"speech_config": [{"voice": req["voice"]}]},
        }
        res = httpx.post(GEMINI_URL, json=body, headers={"x-goog-api-key": key}, timeout=300)
        if res.status_code != 200:
            raise RuntimeError(f"Gemini answered {res.status_code}: {res.text[:600]}")
        data = res.json()
        clips = [c["data"] for s in data.get("steps", []) if s.get("type") == "model_output"
                 for c in s.get("content", []) if c.get("type") == "audio" and c.get("data")]
        if not clips:
            raise RuntimeError(f"Gemini returned no audio: {str(data)[:600]}")
        audio, rate = sf.read(io.BytesIO(base64.b64decode(clips[-1])), dtype="float32")
        if audio.ndim == 2:
            audio = audio.mean(axis=1)
        return change_tempo(audio, req["speed"], rate), rate


def ipa(req: dict) -> dict:
    """Hebrew as stressed IPA, sentence by sentence, the way BlueTTS reads it."""
    import blue_onnx as b

    from pdf2epub_voice.engines.blue import spell_out

    global _g2p
    if _g2p is None:
        _g2p = b.TextProcessor(None)
    pieces = []
    for sentence in chunks(spell_out(strip_niqqud(req["text"])), 300):
        sentence = sentence.replace("【", "").replace("】", "")
        pieces.append(re.sub(r"</?[a-z]{2}>", "", _g2p.phonemize(sentence, "he")).strip())
    say("ready")
    return {"pieces": [p for p in pieces if p]}


if __name__ == "__main__":
    serve({
        "blue": Blue(),
        "bluev3": BlueV3(),
        "chatterbox": Chatterbox(),
        "edge": Edge(),
        "mms": Mms(),
        "gemini": Gemini("gemini-3.8-flash-tts"),
        "gemini-lite": Gemini("gemini-3.8-flash-lite-tts"),
    }, {"ipa": ipa})
