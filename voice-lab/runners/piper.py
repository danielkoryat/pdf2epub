# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#     "huggingface-hub>=0.26",
#     "numpy",
#     "phonikud==0.4.1",
#     "phonikud-onnx>=1.0.6",
#     "piper-onnx>=1.0.6",
#     "soundfile",
# ]
# ///
"""Phonikud's Hebrew voices (Piper models): Phonikud adds the vowels and stress, turns the text into IPA, and a
small Piper model reads it on the processor. All of these voices are licensed for non-commercial use only."""

from __future__ import annotations

from common import Engine, join, lab_dir, serve

# voice: (Hugging Face repository, model file, its config, length scale at normal speed)
VOICES = {
    "shaul": ("Phonikud/phonikud-tts-checkpoints", "shaul.onnx", "model.config.json", 1.2),
    "michael": ("Phonikud/phonikud-tts-checkpoints", "michael.onnx", "model.config.json", 1.2),
    "demo": ("Phonikud/phonikud-tts-checkpoints", "model.onnx", "model.config.json", 1.2),  # their online demo's voice
    "female": ("notmax123/piper-medium-heb", "piper_medium_female.onnx", "model.config.json", 1.0),
    "male": ("notmax123/piper-medium-heb", "piper_medium_male.onnx", "model.config.json", 1.0),
}


class Piper(Engine):
    def load(self):
        from huggingface_hub import hf_hub_download
        from phonikud_onnx import Phonikud

        self.download = hf_hub_download
        self.vowels = Phonikud(hf_hub_download("Phonikud/phonikud-onnx", "phonikud-1.0.int8.onnx"))
        self.models = {}

    def synth(self, req):
        import re

        from phonikud import phonemize
        from piper_onnx import Piper as Model

        repo, model, config, length = VOICES[req["voice"]]
        if req["voice"] not in self.models:
            self.models[req["voice"]] = Model(self.download(repo, model), self.download(repo, config))
        piper = self.models[req["voice"]]
        parts = []
        rate = 22050
        for sentence in re.split(r"(?<=[.!?…])\s+", req["text"].strip()):
            if not sentence:
                continue
            phonemes = phonemize(self.vowels.add_diacritics(sentence))  # keeps Phonikud's stress marks
            samples, rate = piper.create(phonemes, is_phonemes=True, length_scale=length / req["speed"],
                                         noise_scale=0.64, noise_w=1.0)
            parts.append(samples)
        return join(parts, rate), rate


if __name__ == "__main__":
    lab_dir()
    serve({"piper": Piper()})
