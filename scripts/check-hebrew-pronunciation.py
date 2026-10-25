"""Real-model regression check and matched before/after audio, using cached models.

Run with the voice-worker Python, --models <worker models directory>,
--g2p <cached RenikudPlus model.onnx>, and optionally --edge for a Hila sample.
The scores check intended phonemes, not perceived audio quality.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf
from blue_onnx import TextProcessor

from pdf2epub_voice.audio import RATE, silence
from pdf2epub_voice.engines.blue import BlueEngine, MODEL_REVISION, spell_out
from pdf2epub_voice.pronunciation import corrected_processor
from pdf2epub_voice.text import strip_niqqud, tag_latin


CASES = [
    ("רנה דקארט היה פילוסוף צרפתי.", "ʁenˈe dekˈaʁt"),
    ("קראתי על רנה דקארט אתמול.", "ʁenˈe dekˈaʁt"),
    ("החוקר כתב על הומו סאפיינס.", "hˈomo sˈapjens"),
    ("הומו סאפיינס התפשט ברחבי העולם.", "hˈomo sˈapjens"),
]
CONTROLS = [
    "הוא קרא את הספר החדש.",
    "הספר גזר את השיער שלו.",
    "הוא ספר את הכסף.",
    "היא שרה שיר יפה.",
    "שרה הלכה הביתה.",
    "ראש הממשלה הגיע לבית הספר.",
    "לפני כ-70,000 שנה חיו כאן אנשים.",
    "היא פתחה את Windows וקראה באנגלית.",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--g2p", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("out/hebrew-pronunciation"))
    parser.add_argument("--edge", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    engine = BlueEngine(args.models)
    baseline = TextProcessor(str(args.g2p))
    corrected = corrected_processor(baseline, engine.pronunciations)
    report = {"model_revision": MODEL_REVISION, "voice": "noa", "speed": 0.9,
              "scope": "Targeted phoneme checks; not a listening score or general Hebrew accuracy benchmark.",
              "cases": [], "controls": [], "audio": {}}
    for text, expected in CASES:
        prepared = tag_latin(spell_out(strip_niqqud(text)))
        before = baseline.phonemize(prepared, "he")
        after = corrected.phonemize(prepared, "he")
        report["cases"].append({"text": text, "expected": expected, "before": before, "after": after,
                                "before_matches": expected in before, "after_matches": expected in after})
        print(json.dumps(report["cases"][-1], ensure_ascii=False), flush=True)
    for text in CONTROLS:
        prepared = tag_latin(spell_out(strip_niqqud(text)))
        before = baseline.phonemize(prepared, "he")
        after = corrected.phonemize(prepared, "he")
        report["controls"].append({"text": text, "unchanged": before == after})

    # Two names in separate sentences; same voice, speed, steps and random seed.
    demo = [CASES[0][0], CASES[2][0]]
    for label, processor in [("before", baseline), ("after", corrected)]:
        engine.tts.g2p = processor
        parts = []
        for i, text in enumerate(demo):
            np.random.seed(20261023 + i)
            audio = engine.synthesize(text, "he", "noa", 0.9, "smooth")
            if not np.isfinite(audio).all() or not np.any(audio):
                raise RuntimeError(f"Invalid {label} audio")
            parts.extend([audio, silence(0.8)])
        joined = np.concatenate(parts)
        sf.write(args.out / f"{label}.wav", joined, RATE)
        report["audio"][label] = {"seconds": len(joined) / RATE, "peak": float(np.max(np.abs(joined)))}
        print(f"Saved {label}.wav", flush=True)
    if args.edge:
        # One bounded request; no long engine retry loop for an optional comparison.
        import asyncio
        from pdf2epub_voice.engines.edge import EdgeEngine
        try:
            audio = asyncio.run(asyncio.wait_for(EdgeEngine._fetch(" ".join(demo), "he-IL-HilaNeural", "-10%"), timeout=45))
            (args.out / "hila.mp3").write_bytes(audio)
            report["edge"] = "Hila comparison generated; no automatic pronunciation score."
        except Exception as exc:
            report["edge"] = f"Comparison unavailable: {exc}"
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    assert all(case["after_matches"] for case in report["cases"])
    assert all(case["unchanged"] for case in report["controls"])
    print("Targeted phoneme checks and unchanged controls passed. Audio still requires listening review.", flush=True)


if __name__ == "__main__":
    main()
