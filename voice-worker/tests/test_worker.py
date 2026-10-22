"""The voice worker without real voices: text preparation, the M4B, the site API, and reading a whole book with a
tone generator in place of an engine against a fake site."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import numpy as np
import pytest

from pdf2epub_voice.audio import RATE, ChapterAudio, ffmetadata
from pdf2epub_voice.client import Client, Gone
from pdf2epub_voice.reader import Reader, Speeds
from pdf2epub_voice.text import chunks, groups, intro, sentences, strip_niqqud, tag_latin

def _png() -> bytes:
    if not shutil.which("ffmpeg"):
        return b""
    return subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=60x90", "-frames:v", "1", "-c:v", "png",
                           "-f", "image2pipe", "pipe:1"], capture_output=True, check=True).stdout


PNG = _png()
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")


def test_text_preparation():
    assert strip_niqqud("שָׁלוֹם עוֹלָם־גָּדוֹל") == "שלום עולם-גדול"
    assert tag_latin("קראתי את The Old Man and the Sea אתמול") == "קראתי את <en>The Old Man and the Sea</en> אתמול"
    assert sentences("One. Two? \"Three!\" Four") == ["One.", "Two?", "\"Three!\"", "Four"]
    long = "word, " * 100
    assert all(len(c) <= 80 for c in chunks(long, 80)) and "".join(chunks(long, 80)).count("word") == 100
    assert intro("הזר", ["אלבר קאמי"], "he") == "הזר. מאת אלבר קאמי."


def test_chapter_marks():
    meta = ffmetadata("A; B", ["X=Y"], [ChapterAudio("One", Path("a"), 1.5), ChapterAudio("Two", Path("b"), 2.0)], "en")
    assert "title=A\\; B" in meta and "artist=X\\=Y" in meta
    assert "START=0\nEND=1500\ntitle=One" in meta and "START=1500\nEND=3500\ntitle=Two" in meta


class Tone:
    """An engine that 'reads' by beeping: 20 ms per letter."""

    name = "blue"
    group_chars = 0

    def __init__(self):
        self.calls = []

    def synthesize(self, text, lang, voice, speed, quality):
        self.calls.append((text, lang))
        t = np.arange(int(len(text) * 0.02 * RATE)) / RATE
        return (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


class Engines:
    def __init__(self, engine):
        self.engine = engine

    def get(self, name):
        return self.engine


SCRIPT = {"format": 1, "title": "Test Book", "authors": ["A. Writer"], "language": "en", "chapters": [
    {"title": "One", "paragraphs": [{"text": "The first paragraph.", "lang": "en", "kind": "p"},
                                    {"text": "", "lang": "", "kind": "break"},
                                    {"text": "שלום עולם", "lang": "he", "kind": "p"}]},
    {"title": "", "paragraphs": [{"text": "The second chapter has no title.", "lang": "en", "kind": "p"}]},
]}


class FakeSite:
    def __init__(self):
        self.parts, self.finished, self.progress, self.deleted = {}, None, [], False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.deleted and "/jobs/" in path:
            return httpx.Response(404, json={"detail": "No such job"})
        if path == "/api/voice/claim":
            return httpx.Response(200, json={"id": "job1", "title": "Test Book", "options": {"engine": "blue", "voice": "noa", "speed": 1.0, "quality": "smooth"},
                                             "has_cover": True, "attempts": 1})
        if path.endswith("/cover"):
            return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})
        if path.endswith("/script"):
            return httpx.Response(200, json=SCRIPT)
        if path.endswith("/progress"):
            self.progress.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if "/parts/" in path:
            self.parts[int(path.rsplit("/", 1)[1])] = request.content
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/finish"):
            self.finished = json.loads(request.content)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(500)


def test_client_retries_and_stops_on_gone(monkeypatch):
    monkeypatch.setattr("pdf2epub_voice.client.time.sleep", lambda s: None)
    calls = []

    def flaky(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(502)
        return httpx.Response(204) if request.url.path.endswith("claim") else httpx.Response(404)

    client = Client("https://site.test", "t" * 40, "laptop", transport=httpx.MockTransport(flaky))
    assert client.claim(["blue"], {}) is None and len(calls) == 2
    with pytest.raises(Gone):
        client.progress("job1", 1, 2, [])


@needs_ffmpeg
def test_reading_a_book(tmp_path: Path):
    site = FakeSite()
    tone = Tone()
    client = Client("https://site.test", "t" * 40, "laptop", transport=httpx.MockTransport(site))
    speeds = Speeds(tmp_path / "speeds.json")
    Reader(client, Engines(tone), tmp_path, speeds).read(client.claim(["blue"], {}))
    assert site.finished and site.finished["chapters"] == 2
    assert list(speeds.totals) == ["blue/smooth"]
    data = b"".join(site.parts[i] for i in sorted(site.parts))
    assert hashlib.sha256(data).hexdigest() == site.finished["sha256"] and data[4:8] == b"ftyp"
    spoken = [t for t, _ in tone.calls]
    assert spoken[0] == "Test Book. By A. Writer." and "One" in spoken and "Chapter 2" not in spoken
    assert ("שלום עולם", "he") in tone.calls
    m4b = tmp_path / "out.m4b"
    m4b.write_bytes(data)
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-show_format", "-show_streams", "-of", "json", str(m4b)],
                                      capture_output=True, text=True).stdout)
    assert [c["tags"]["title"] for c in probe["chapters"]] == ["One", "Chapter 2"]
    assert probe["format"]["tags"]["title"] == "Test Book" and probe["format"]["tags"]["genre"] == "Audiobook"
    assert any(s["codec_type"] == "video" and s["disposition"]["attached_pic"] for s in probe["streams"])  # the cover
    assert not any(tmp_path.glob("job1-*"))  # the work folder is cleaned up


def test_speeds_are_measured_and_kept(tmp_path: Path):
    speeds = Speeds(tmp_path / "speeds.json")
    speeds.add("blue/smooth", 30, 60)
    assert speeds.ratios() == {}  # too little to go by
    speeds.add("blue/smooth", 90, 240)
    assert Speeds(tmp_path / "speeds.json").ratios() == {"blue/smooth": 0.4}


@needs_ffmpeg
def test_a_deleted_job_stops(tmp_path: Path):
    site = FakeSite()
    client = Client("https://site.test", "t" * 40, "laptop", transport=httpx.MockTransport(site))
    job = client.claim(["blue"], {})
    tone = Tone()
    original = tone.synthesize

    def delete_midway(*a):
        site.deleted = True
        return original(*a)

    tone.synthesize = delete_midway
    reader = Reader(client, Engines(tone), tmp_path)
    try:
        reader.read(job)
    except Gone:
        pass
    assert site.finished is None and not site.parts


def test_blue_reads_in_sentence_groups():
    # BlueTTS splits only at . ! ?, and its models refuse long input: a 1,800-letter sentence without a full stop
    # failed a whole book. It gets sentence groups of at most PIECE_CHARS letters.
    from pdf2epub_voice.engines.blue import PIECE_CHARS, BlueEngine

    asked: list[str] = []

    def tts(text, **kwargs):
        asked.append(text)
        return np.ones((1, 100), dtype=np.float32), None

    engine = object.__new__(BlueEngine)
    engine.tts, engine.rate, engine._styles = tts, RATE, {"noa": None}
    clause = "ושל ההיסטוריה של העולם כולו, כפי שסופרה בעיר New York"
    sentence = " ".join([clause] * 40) + "."  # about 2,200 letters, no full stop until the end
    audio = engine.synthesize(sentence, "he", "noa", 1.0, "smooth")
    assert len(asked) > 5 and all(len(a.replace("<en>", "").replace("</en>", "")) <= PIECE_CHARS for a in asked)
    assert all(a.count("<en>") == a.count("</en>") for a in asked)  # tags are added after cutting, never cut in half
    assert len(audio) > len(asked) * 100  # the pieces, with a pause between each
    asked.clear()
    engine.synthesize("משפט קצר. ועוד אחד!", "he", "noa", 1.0, "smooth")
    assert asked == ["משפט קצר. ועוד אחד!"]  # short sentences still go together


def test_sentence_groups():
    text = "One. Two is longer, with a comma. Three!"
    assert groups(text, 100) == [text]
    small = groups(text, 20)
    assert all(len(g) <= 20 for g in small) and " ".join(small) == text  # nothing lost or reordered
    assert all(len(g) <= 30 for g in groups("word " * 50, 30))


@needs_ffmpeg
def test_a_stuck_engine_ends_the_worker_to_be_started_again(tmp_path: Path, monkeypatch):
    # A worker once hung inside a library while its heartbeats went on: nothing read for too long ends the process.
    import threading

    from pdf2epub_voice import reader as reader_module

    site = FakeSite()
    client = Client("https://site.test", "t" * 40, "laptop", transport=httpx.MockTransport(site))
    released, exits = threading.Event(), []
    monkeypatch.setattr(reader_module, "PROGRESS_SECONDS", 0.05)
    monkeypatch.setattr(reader_module, "_exit", lambda code: (exits.append(code), released.set()))
    tone = Tone()
    original = tone.synthesize

    def hang_once(*a):
        if not tone.calls:
            released.wait(10)  # stuck until the watchdog acts (here it lets go instead of ending the tests)
        return original(*a)

    tone.synthesize = hang_once
    Reader(client, Engines(tone), tmp_path, stall_seconds=0.3).read(client.claim(["blue"], {}))
    assert exits == [reader_module.EXIT_STALLED]
    assert any("starting the voice worker again" in line for p in site.progress for line in p["log"])



def test_speed_is_measured_over_the_last_minutes(monkeypatch):
    from pdf2epub_voice import reader as reader_module
    from pdf2epub_voice.reader import SPEED_WINDOW, _Progress

    now = [1000.0]
    monkeypatch.setattr(reader_module.time, "monotonic", lambda: now[0])
    progress = _Progress(None, "job", 0)
    progress.made(30)
    assert progress.speed() is None  # nothing to go by yet
    now[0] += 20
    progress.made(40)
    now[0] += 20
    progress.made(40)
    assert progress.speed() == 2.0  # 80 s of audio in the 40 s after the first paragraph
    now[0] += SPEED_WINDOW + 1
    progress.made(10)
    assert progress.speed() is None  # older paragraphs no longer count


@needs_ffmpeg
def test_m4b_is_built_while_the_worker_waits_on_its_input(tmp_path: Path):
    # The local app holds the worker's standard input open, and a worker thread waits on it. An ffmpeg that
    # inherited that input hung for good on Windows before finishing a book's M4B.
    import sys

    child = tmp_path / "child.py"
    child.write_text("""
import sys, threading, time
from pathlib import Path
import numpy as np, soundfile as sf
from pdf2epub_voice.audio import RATE, ChapterAudio, build_m4b
threading.Thread(target=lambda: sys.stdin.buffer.read(), daemon=True).start()
time.sleep(0.5)
work = Path(sys.argv[1])
chapters = []
for n in range(2):
    path = work / f"{n}.flac"
    noise = np.random.default_rng(n).uniform(-0.3, 0.3, RATE * 600).astype(np.float32)  # takes ffmpeg seconds: it looks for keys
    sf.write(str(path), noise, RATE, subtype="PCM_16")
    chapters.append(ChapterAudio(f"Chapter {n + 1}", path, 600.0))
build_m4b(chapters, "Book", ["A. Writer"], "en", None, work / "book.m4b")
print("built", flush=True)
""", encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(child), str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        p.wait(timeout=60)  # the input stays open meanwhile, as the app keeps it
    except subprocess.TimeoutExpired:
        p.kill()
        pytest.fail("building the M4B hung")
    finally:
        p.stdin.close()
    assert b"built" in p.stdout.read() and (tmp_path / "book.m4b").stat().st_size > 0
