"""Audiobooks: the text to read from an EPUB, the audio job's life on the server, and the voice worker's API."""

import dataclasses
import hashlib
import shutil
import time
from pathlib import Path

from fastapi.testclient import TestClient
from test_enrich import ENG, HEB, epub3_hebrew
from test_server_mode import BASE, Site, make_epub

from pdf2epub.model import NOTE_CLOSE, NOTE_OPEN, Block, Book, Chapter, Kind
from pdf2epub.narration import load_script, narration_script, save_script
from pdf2epub.voices import estimate, validate_options
from pdf2epub.web.server import create_app
from pdf2epub.web.worker import run_action
from pdf2epub.writer import write_epub

TOKEN = "v" * 40
M4B = b"\x00\x00\x00\x20ftypM4B \x00\x00\x02\x00" + b"\x00" * 5000


def _book(tmp: Path) -> bytes:
    note = Block(Kind.FOOTNOTE, text="This note is never read.", page=2, note_id="note-1", marker="1", linked=True)
    one = Chapter("פרק ראשון", [
        Block(Kind.HEADING, text="פרק ראשון", page=1, level=1),
        Block(Kind.PARAGRAPH, text=HEB + f"{NOTE_OPEN}note-1{NOTE_CLOSE}", page=1),
        Block(Kind.PARAGRAPH, text="* * *", page=1),
        Block(Kind.PARAGRAPH, text=ENG, page=2),
    ], notes=[note])
    two = Chapter("פרק שני", [Block(Kind.HEADING, text="פרק שני", page=3, level=1), Block(Kind.PARAGRAPH, text=HEB, page=3)])
    book = Book(title="ספר בדיקה", authors=["כותבת"], language="he", direction="rtl", chapters=[one, two])
    path = tmp / "book.epub"
    write_epub(book, path)
    return path.read_bytes()


def test_narration_of_our_own_epub(tmp_path: Path):
    script = narration_script(_book(tmp_path))
    assert script.title == "ספר בדיקה" and script.authors == ["כותבת"] and script.language == "he"
    assert [ch.title for ch in script.chapters] == ["פרק ראשון", "פרק שני"]
    first = script.chapters[0].paragraphs
    assert [p.kind for p in first] == ["p", "break", "p"]  # the title heading is not read twice
    assert first[0].lang == "he" and first[2].lang == "en"  # the English paragraph is read in English
    text = " ".join(p.text for ch in script.chapters for p in ch.paragraphs)
    assert "never read" not in text and NOTE_OPEN not in text and "1" not in first[0].text[-3:]
    save_script(script, tmp_path / "script.json")
    assert load_script(tmp_path / "script.json") == script


def test_narration_splits_files_at_contents_anchors():
    from test_enrich import _zip

    body = ('<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
            '<head><title>x</title></head><body>'
            '<h2 id="c1">One</h2><p>The first chapter.<a epub:type="noteref" href="#n1">1</a></p>'
            '<div><h2 id="c2">Two</h2><p>The second chapter.<sup>2</sup></p></div>'
            '<aside epub:type="footnote" id="n1"><p>A footnote.</p></aside><span epub:type="pagebreak" title="7">7</span>'
            '<table><tr><td>a table</td></tr></table></body></html>')
    nav = ('<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
           '<head><title>nav</title></head><body><nav epub:type="toc"><ol><li><a href="text.xhtml#c1">One</a></li>'
           '<li><a href="text.xhtml#c2">Two</a></li></ol></nav></body></html>')
    opf = ('<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Anchors</dc:title><dc:language>en</dc:language>'
           '<dc:identifier id="id">x</dc:identifier></metadata><manifest>'
           '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
           '<item id="t" href="text.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="nav"/><itemref idref="t"/></spine></package>')
    script = narration_script(_zip({"OEBPS/content.opf": opf, "OEBPS/nav.xhtml": nav, "OEBPS/text.xhtml": body}))
    assert [(ch.title, [p.text for p in ch.paragraphs]) for ch in script.chapters] == [
        ("One", ["The first chapter."]), ("Two", ["The second chapter."])]


def test_options_are_checked():
    assert validate_options({"engine": "blue", "voice": "noa", "speed": 0.9, "quality": "smooth"}, "he-IL")["language"] == "he"
    assert validate_options({"engine": "edge", "voice": "en-US-AvaNeural", "speed": 1}, "en")["quality"] == "standard"
    for bad, lang in (({"engine": "edge", "voice": "he-IL-HilaNeural", "speed": 1}, "en"),  # a Hebrew voice for English
                      ({"engine": "blue", "voice": "noa", "speed": 3}, "he"),
                      ({"engine": "nope", "voice": "noa"}, "he"),
                      ({"engine": "blue", "voice": "noa"}, "fr")):
        try:
            validate_options(bad, lang)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad} for {lang}")


# -- server mode -------------------------------------------------------------------------------------------


def _done_book(site: Site, c: TestClient) -> str:
    """A finished EPUB job (the worker is not running in these tests: its result is put in place by hand)."""
    job = site.upload(c, "book.epub", make_epub(), "application/epub+zip").json()
    library = site.app.state.library
    path = library.dir(job["id"])
    shutil.copyfile(path / "input.epub", path / "book.epub")
    site.db.update_job(job["id"], status="done", pending=None, language="he", title="ספר")
    return job["id"]


def _voice(site: Site, token: str = TOKEN, name: str = "laptop") -> TestClient:
    return TestClient(site.app, base_url=BASE, headers={"Authorization": f"Bearer {token}", "X-Voice-Worker": name})


def _site(tmp_path: Path, **kw) -> Site:
    site = Site(tmp_path)
    settings = dataclasses.replace(site.settings, voice_token=TOKEN, **kw)
    site.settings = settings
    site.app = create_app(settings=settings, start_worker=False)
    site.db = site.app.state.db
    return site


def _prepare(site: Site, job_id: str) -> None:
    """What the server's worker does with a new audio job."""
    row = site.db.claim_job("test")
    assert row and row["id"] == job_id and row["pending"] == "prepare"
    run_action(site.settings, site.db, site.app.state.library, job_id)


def test_audiobooks_are_for_admins_by_default(tmp_path: Path):
    site = _site(tmp_path)
    user = site.client("someone@example.com")
    job_id = _done_book(site, user)
    assert user.get("/api/me").json()["audiobooks"] is False
    assert user.get("/api/voices").status_code == 404
    assert user.post(f"/api/jobs/{job_id}/audiobook", json={"engine": "blue", "voice": "noa"}).status_code == 404

    site = _site(tmp_path / "open", audiobooks="everyone")
    user = site.client("someone@example.com")
    job_id = _done_book(site, user)
    assert user.get("/api/me").json()["audiobooks"] is True
    res = user.post(f"/api/jobs/{job_id}/audiobook", json={"engine": "blue", "voice": "noa", "speed": 0.9, "quality": "smooth"})
    assert res.status_code == 200, res.text
    again = user.post(f"/api/jobs/{job_id}/audiobook", json={"engine": "blue", "voice": "noa", "speed": 0.9, "quality": "smooth"})
    assert again.status_code == 429  # one audiobook a day
    assert len(user.get("/api/jobs").json()) == 2


def test_audiobook_from_request_to_download(tmp_path: Path):
    site = _site(tmp_path)
    admin = site.client("admin@example.com")
    source = _done_book(site, admin)
    res = admin.post(f"/api/jobs/{source}/audiobook", json={"engine": "blue", "voice": "lily", "speed": 1.1, "quality": "fast"})
    assert res.status_code == 200, res.text
    audio = res.json()
    assert audio["kind"] == "audio" and audio["options"]["voice"] == "lily" and audio["options"]["source"] == source
    assert audio["options"]["quality"] == "smooth"  # the only quality BlueTTS offers
    assert admin.post(f"/api/jobs/{source}/audiobook", json={"engine": "edge", "voice": "en-US-AvaNeural"}).status_code == 400

    _prepare(site, audio["id"])
    job = admin.get(f"/api/jobs/{audio['id']}").json()
    assert job["status"] == "queued" and job["pending"] == "speak", job
    assert job["seconds_left"] == job["summary"]["making_seconds"]  # nothing read yet, no worker has measured
    assert job["summary"]["chapters"] == 2 and job["summary"]["characters"] > 500

    # The voice worker: a token is required, no Origin is needed, and claims go by engine.
    assert _voice(site, token="x" * 40).post("/api/voice/claim", json={"engines": ["blue"]}).status_code == 401
    assert TestClient(site.app, base_url=BASE).post("/api/voice/claim", json={"engines": ["blue"]}).status_code == 401
    voice = _voice(site)
    assert voice.post("/api/voice/claim", json={"engines": ["edge"]}).status_code == 204
    claimed = voice.post("/api/voice/claim", json={"engines": ["blue", "edge"], "info": {"gpu": "test", "speeds": {"blue/smooth": 10.0}}})
    assert claimed.status_code == 200 and claimed.json()["id"] == audio["id"]
    assert admin.get("/api/voices").json()["online"] is True

    script = voice.get(f"/api/voice/jobs/{audio['id']}/script").json()
    assert [ch["title"] for ch in script["chapters"]] == ["פרק א", "פרק ב"]
    other = _voice(site, name="someone-else")
    assert other.post(f"/api/voice/jobs/{audio['id']}/progress", json={"done": 1, "total": 2}).status_code == 409
    assert voice.post(f"/api/voice/jobs/{audio['id']}/progress", json={"done": 1, "total": 4, "log": ["Chapter 1 of 2"]}).status_code == 200
    job = admin.get(f"/api/jobs/{audio['id']}").json()
    assert job["status"] == "building" and job["done"] == 1 and job["stage_label"] == "Reading aloud"
    # Time left: what is left to read, at the speed this worker measured (10x real time).
    assert voice.post(f"/api/voice/jobs/{audio['id']}/progress", json={"done": 1_000, "total": 29_000}).status_code == 200
    job = admin.get(f"/api/jobs/{audio['id']}").json()
    assert job["seconds_left"] == estimate(28_000, job["options"], {"blue/smooth": 10.0})["making_seconds"] > 0
    assert job["speed"] == 10.0
    # Once the worker sends the speed of its last minutes, that is what counts.
    assert voice.post(f"/api/voice/jobs/{audio['id']}/progress", json={"done": 1_000, "total": 29_000, "speed": 4.0}).status_code == 200
    job = admin.get(f"/api/jobs/{audio['id']}").json()
    assert job["speed"] == 4.0 and job["seconds_left"] == estimate(28_000, job["options"], {"blue/smooth": 4.0})["making_seconds"]

    half = len(M4B) // 2
    assert voice.put(f"/api/voice/jobs/{audio['id']}/parts/0", content=M4B[:half]).status_code == 200
    assert voice.put(f"/api/voice/jobs/{audio['id']}/parts/1", content=M4B[half:]).status_code == 200
    bad = {"parts": 2, "sha256": "0" * 64, "seconds": 61.5, "chapters": 2}
    assert voice.post(f"/api/voice/jobs/{audio['id']}/finish", json=bad).status_code == 400
    good = {**bad, "sha256": hashlib.sha256(M4B).hexdigest()}
    assert voice.post(f"/api/voice/jobs/{audio['id']}/finish", json=good).status_code == 200
    job = admin.get(f"/api/jobs/{audio['id']}").json()
    assert job["status"] == "done" and job["report"]["seconds"] == 62
    download = admin.get(f"/api/jobs/{audio['id']}/audiobook")
    assert download.status_code == 200 and download.content == M4B and download.headers["content-type"] == "audio/mp4"
    assert ".m4b" in download.headers["content-disposition"]
    assert site.db.audit_rows(action="voice.finish")


def test_deleting_while_reading_stops_the_worker(tmp_path: Path):
    site = _site(tmp_path)
    admin = site.client("admin@example.com")
    audio = admin.post(f"/api/jobs/{_done_book(site, admin)}/audiobook", json={"engine": "blue", "voice": "noa"}).json()
    _prepare(site, audio["id"])
    voice = _voice(site)
    assert voice.post("/api/voice/claim", json={"engines": ["blue"]}).status_code == 200
    assert admin.delete(f"/api/jobs/{audio['id']}").status_code == 204
    assert voice.post(f"/api/voice/jobs/{audio['id']}/progress", json={"done": 1, "total": 2}).status_code == 404


def test_a_sleeping_voice_worker_gets_its_job_back(tmp_path: Path):
    site = _site(tmp_path)
    admin = site.client("admin@example.com")
    audio = admin.post(f"/api/jobs/{_done_book(site, admin)}/audiobook", json={"engine": "blue", "voice": "noa"}).json()
    _prepare(site, audio["id"])
    for _ in range(4):  # more than the two attempts a conversion gets
        assert _voice(site).post("/api/voice/claim", json={"engines": ["blue"]}).status_code == 200
        site.db.update_job(audio["id"], heartbeat=time.time() - 1000)
        assert site.db.requeue_stale(180) == [audio["id"]]
        assert site.db.job(audio["id"])["status"] == "queued"
    # The server's own worker never takes a job that waits for voices.
    assert site.db.claim_job("server-worker") is None


def test_only_engines_the_voice_worker_has_are_accepted(tmp_path: Path):
    site = _site(tmp_path)
    admin = site.client("admin@example.com")
    source = _done_book(site, admin)
    chatterbox = {"engine": "chatterbox", "voice": "noa"}
    # With no voice worker online, any engine may wait for one.
    first = admin.post(f"/api/jobs/{source}/audiobook", json=chatterbox)
    assert first.status_code == 200, first.text
    _prepare(site, first.json()["id"])
    # The server's voice worker has no GPU: it leaves that one alone, and no more are taken.
    info = {"speeds": {"blue/smooth": 0.45, "bad": "x"}}
    assert _voice(site, name="server").post("/api/voice/claim", json={"engines": ["blue", "edge"], "info": info}).status_code == 204
    voices = admin.get("/api/voices").json()
    assert voices["online_engines"] == ["blue", "edge"]
    blue = next(e for e in voices["engines"] if e["id"] == "blue")
    assert {q["id"]: q["factor"] for q in blue["qualities"]} == {"smooth": 0.45}  # as measured
    assert admin.post(f"/api/jobs/{source}/audiobook", json=chatterbox).status_code == 400
    blue = admin.post(f"/api/jobs/{source}/audiobook", json={"engine": "blue", "voice": "noa", "speed": 1, "quality": "smooth"})
    assert blue.status_code == 200
    _prepare(site, blue.json()["id"])
    summary = admin.get(f"/api/jobs/{blue.json()['id']}").json()["summary"]
    assert abs(summary["making_seconds"] - summary["audio_seconds"] / 0.45) < 3  # the estimate uses the measured speed


def test_voice_api_is_off_without_a_token(tmp_path: Path):
    site = Site(tmp_path)  # server mode, no PDF2EPUB_VOICE_TOKEN
    assert TestClient(site.app, base_url=BASE).post("/api/voice/claim", json={"engines": ["blue"]}).status_code == 404


def test_local_mode_end_to_end(tmp_path: Path):
    """Locally the voice worker needs no token, and the in-process worker prepares the text."""
    app = create_app(tmp_path)
    c = TestClient(app)
    job = c.post("/api/jobs", files={"file": ("b.epub", epub3_hebrew("he"), "application/epub+zip")}, data={"review": "false"}).json()
    deadline = time.time() + 60
    while c.get(f"/api/jobs/{job['id']}").json()["status"] != "done":
        assert time.time() < deadline
        time.sleep(0.2)
    audio = c.post(f"/api/jobs/{job['id']}/audiobook", json={"engine": "edge", "voice": "he-IL-HilaNeural", "speed": 1}).json()
    while (j := c.get(f"/api/jobs/{audio['id']}").json())["pending"] != "speak":
        assert time.time() < deadline and j["status"] != "failed", j
        time.sleep(0.2)
    assert c.post("/api/voice/claim", json={"engines": ["edge"]}).json()["id"] == audio["id"]
    app.state.worker.stop.set()


def test_a_failed_audiobook_is_tried_again_as_the_same_job(tmp_path: Path):
    # The same job, so the voice worker continues from the chapters it already made; a new request starts over.
    site = _site(tmp_path)
    admin = site.client("admin@example.com")
    source = _done_book(site, admin)
    audio = admin.post(f"/api/jobs/{source}/audiobook", json={"engine": "blue", "voice": "noa"}).json()
    assert admin.post(f"/api/jobs/{source}/retry").status_code == 409  # not an audiobook
    _prepare(site, audio["id"])
    assert admin.post(f"/api/jobs/{audio['id']}/retry").status_code == 409  # not failed
    voice = _voice(site)
    assert voice.post("/api/voice/claim", json={"engines": ["blue"]}).json()["id"] == audio["id"]
    assert voice.post(f"/api/voice/jobs/{audio['id']}/progress", json={"done": 1_000, "total": 4_000}).status_code == 200
    assert voice.post(f"/api/voice/jobs/{audio['id']}/fail", json={"message": "invalid expand shape"}).status_code == 200
    assert admin.get(f"/api/jobs/{audio['id']}").json()["status"] == "failed"

    retried = admin.post(f"/api/jobs/{audio['id']}/retry")
    assert retried.status_code == 200 and retried.json()["status"] == "queued" and retried.json()["error"] == ""
    assert retried.json()["pending"] == "speak"  # its text is ready: straight back to the voice worker
    left = retried.json()["seconds_left"]  # what is left at the speed the workers measured, not the first estimate
    assert left == estimate(3_000, audio["options"], {})["making_seconds"] > 0
    assert voice.post("/api/voice/claim", json={"engines": ["blue"]}).json()["id"] == audio["id"]
    assert site.db.audit_rows(action="job.retry")


def test_notes_and_index_are_not_read():
    from pdf2epub import narration as n

    def chapter(title, text, times=1):
        return n.Chapter(title, [n.Paragraph(text, "he")] * times)

    prose = "המהפכה החקלאית שינתה את חייהם של בני האדם לפני כעשרת אלפים שנה, ולא תמיד לטובה. " * 30
    book = [chapter("ציר הזמן", "13.5 מיליארד שנה המפץ הגדול. 70,000 שנה המהפכה הלשונית."),
            chapter("פרק 1", prose), chapter("פרק 2", prose), chapter("פרק 3", prose),
            chapter('DIPD "XID', "Hassan, Demographic Archaeology (1981), 196-9; 2001, 143. ", 40),  # garbled "הערות"
            chapter("מפתח השמות והעניינים", "חקלאות 105-104, 108, 109, 111 תנאי חיים 86, 87, 88 ", 40)]
    assert [c.title for c in n._without_reference_matter(book)] == ["ציר הזמן", "פרק 1", "פרק 2", "פרק 3"]
    assert [c.title for c in n._without_reference_matter([chapter("הערות", prose), *book[1:4]])][0] == "הערות"  # early: kept
