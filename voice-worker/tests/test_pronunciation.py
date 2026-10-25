import json

import pytest

from pdf2epub_voice.pronunciation import DEFAULTS, Pronunciations, corrected_processor


def test_phrase_correction_keeps_context_punctuation_and_repeated_occurrences():
    rules = Pronunciations({"רנה דקארט": "ʁenˈe dekˈaʁt"})
    text = "רנה דקארט, ואז (רנה דקארט)."
    assert rules.apply(text, "ʁˈana dekˈaʁt, veʔˈaz (ʁˈana dekˈaʁt).") == "ʁenˈe dekˈaʁt, veʔˈaz (ʁenˈe dekˈaʁt)."


@pytest.mark.parametrize("text", ["רנה הלכה הביתה.", "ורנה דקארט", "רנה דקארטים", "רנה-דקארט"])
def test_no_global_name_or_substring_replacement(text):
    assert Pronunciations(DEFAULTS).apply(text, "unchanged") == "unchanged"


def test_longest_phrase_wins_and_alignment_failure_is_safe():
    rules = Pronunciations({"ספר": "sˈefeʁ", "בית ספר": "bˈet sˈefeʁ"})
    assert rules.apply("בית ספר", "bˈajit safˈaʁ") == "bˈet sˈefeʁ"
    assert rules.apply("בית ספר", "one-token") == "one-token"
    assert rules.apply("ב-ספר", "be-safˈaʁ") == "be-safˈaʁ"


@pytest.mark.parametrize("entries", [[], {"": "a"}, {"רנה": ""}, {"רנה": "<en>test</en>"}, {"רנה": "רנה"}, {"רנה דקארט": "one"}])
def test_invalid_dictionary_is_rejected(entries):
    with pytest.raises(ValueError):
        Pronunciations(entries)


def test_custom_dictionary_overrides_defaults_and_changes_cache_key(tmp_path, monkeypatch):
    monkeypatch.delenv("PDF2EPUB_HEBREW_PRONUNCIATIONS", raising=False)
    original = Pronunciations.load()
    path = tmp_path / "pronunciations.json"
    path.write_text(json.dumps({"רנה דקארט": "ʁenˈe dekaʁt"}), encoding="utf-8")
    monkeypatch.setenv("PDF2EPUB_HEBREW_PRONUNCIATIONS", str(path))
    custom = Pronunciations.load()
    assert custom.entries["רנה דקארט"] == "ʁenˈe dekaʁt"
    assert custom.fingerprint != original.fingerprint
    assert custom.fingerprint == Pronunciations(dict(reversed(list(custom.entries.items())))).fingerprint


def test_processor_keeps_sentence_context_and_mixed_english():
    calls = []

    class Base:
        def _phonemize_segment(self, text, lang):
            calls.append((text, lang))
            return {"he": "ʁˈana dekˈaʁt katˈav.", "en": "hello"}[lang]

    processor = corrected_processor(Base(), Pronunciations(DEFAULTS))
    assert processor.phonemize("רנה דקארט כתב. <en>Hello</en>", "he") == "<he>ʁenˈe dekˈaʁt katˈav.</he> <en>hello</en>"
    assert calls == [("רנה דקארט כתב. ", "he"), ("Hello", "en")]
