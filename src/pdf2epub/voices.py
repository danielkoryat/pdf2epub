"""The voices an audiobook can be read with, and checking a request for one.

The voices themselves are made by the voice worker (voice-worker/ in this repository), on a computer with a GPU
outside the server. This catalog is what the web app offers and accepts; the worker knows how to make each one.

Engines:
  * blue: BlueTTS (open source, MIT code). Hebrew and English. Runs on the processor: about 5x real time at the
    "fast" quality and 2.5x at "smooth" (32 refinement steps instead of 5).
  * chatterbox: Chatterbox Multilingual (open source, MIT). Hebrew and English, on the GPU at about real time.
    Its voices are copied from a short BlueTTS recording of the voice of the same name.
  * edge: Microsoft Edge's online read-aloud voices. Free and fast, but an unofficial service that may change.
"""

from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).parent / "web" / "static"

# Letters spoken per second at normal speed (measured on a Hebrew novel and English samples), to estimate an
# audiobook's length.
CHARS_PER_SECOND = {"he": 14.0, "en": 16.0}

ENGINES: dict[str, dict] = {
    "blue": {
        "label": "BlueTTS",
        "about": "Open source. Natural Hebrew, reads numbers and English words.",
        "languages": ["he", "en"],
        "voices": [
            {"id": "noa", "label": "Noa", "gender": "female"},
            {"id": "lily", "label": "Lily", "gender": "female"},
            {"id": "daniel", "label": "Daniel", "gender": "male"},
            {"id": "adam", "label": "Adam", "gender": "male"},
        ],
        "qualities": {"smooth": 2.5, "fast": 5.0},  # quality -> times faster than real time
    },
    "chatterbox": {
        "label": "Chatterbox",
        "about": "Open source, a larger model on the GPU. Livelier, slower to make.",
        "languages": ["he", "en"],
        "voices": [
            {"id": "default", "label": "Chatterbox", "gender": "female"},
            {"id": "noa", "label": "Noa", "gender": "female"},
            {"id": "lily", "label": "Lily", "gender": "female"},
            {"id": "daniel", "label": "Daniel", "gender": "male"},
            {"id": "adam", "label": "Adam", "gender": "male"},
        ],
        "qualities": {"standard": 0.9},
    },
    "edge": {
        "label": "Microsoft Edge voices",
        "about": "Microsoft's free online voices. Fast; an unofficial service that may stop working.",
        "languages": ["he", "en"],
        "voices": [
            {"id": "he-IL-HilaNeural", "label": "Hila", "gender": "female", "language": "he"},
            {"id": "he-IL-AvriNeural", "label": "Avri", "gender": "male", "language": "he"},
            {"id": "en-US-AvaNeural", "label": "Ava (US)", "gender": "female", "language": "en"},
            {"id": "en-US-AndrewNeural", "label": "Andrew (US)", "gender": "male", "language": "en"},
            {"id": "en-GB-SoniaNeural", "label": "Sonia (UK)", "gender": "female", "language": "en"},
            {"id": "en-GB-RyanNeural", "label": "Ryan (UK)", "gender": "male", "language": "en"},
        ],
        "qualities": {"standard": 30.0},
    },
}
QUALITY_LABELS = {"smooth": "Smooth (slower to make)", "fast": "Fast", "standard": "Standard"}
SPEEDS = (0.8, 0.9, 1.0, 1.1, 1.2)
DEFAULT = {"engine": "blue", "voice": "noa", "speed": 0.9, "quality": "smooth"}


def voice_ok(engine: str, voice: dict, language: str) -> bool:
    return language in ENGINES[engine]["languages"] and voice.get("language", language) == language


def sample_url(engine: str, voice: str, language: str) -> str | None:
    name = f"{engine}-{voice}-{language}.mp3"
    return f"/static/voices/{name}" if (STATIC / "voices" / name).is_file() else None


def catalog() -> dict:
    """The engines and voices for the browser, with a sample to play where one exists."""
    engines = []
    for key, engine in ENGINES.items():
        voices = []
        for v in engine["voices"]:
            langs = [v["language"]] if "language" in v else engine["languages"]
            voices.append({**v, "languages": langs, "samples": {lang: sample_url(key, v["id"], lang) for lang in langs}})
        qualities = [{"id": q, "label": QUALITY_LABELS[q], "factor": f} for q, f in engine["qualities"].items()]
        engines.append({"id": key, "label": engine["label"], "about": engine["about"], "languages": engine["languages"],
                        "voices": voices, "qualities": qualities})
    return {"engines": engines, "speeds": list(SPEEDS), "default": DEFAULT}


def validate_options(data: dict, language: str) -> dict:
    """The audiobook options of a request, checked against the catalog. Raises ValueError with a message for
    the user."""
    language = (language or "").split("-")[0].lower()
    if language not in CHARS_PER_SECOND:
        raise ValueError("Audiobooks can be made of Hebrew and English books.")
    engine = data.get("engine", DEFAULT["engine"])
    if engine not in ENGINES:
        raise ValueError("Unknown voice engine.")
    spec = ENGINES[engine]
    voice = next((v for v in spec["voices"] if v["id"] == data.get("voice")), None)
    if voice is None or not voice_ok(engine, voice, language):
        raise ValueError("That voice can't read this book's language.")
    try:
        speed = float(data.get("speed", DEFAULT["speed"]))
    except (TypeError, ValueError):
        raise ValueError("Unknown reading speed.") from None
    if speed not in SPEEDS:
        raise ValueError("Unknown reading speed.")
    quality = data.get("quality") or next(iter(spec["qualities"]))
    if quality not in spec["qualities"]:
        quality = next(iter(spec["qualities"]))
    return {"engine": engine, "voice": voice["id"], "speed": speed, "quality": quality, "language": language}


def estimate(characters: int, options: dict) -> dict:
    """How long the audiobook will be, and how long making it will take (seconds)."""
    length = characters / CHARS_PER_SECOND[options["language"]] / options["speed"]
    factor = ENGINES[options["engine"]]["qualities"][options["quality"]]
    return {"audio_seconds": round(length), "making_seconds": round(length / factor)}
