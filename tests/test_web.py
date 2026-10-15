import io
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pdf2epub.edit import ChapterEdit, Edits, apply_edits, chapter_summaries
from pdf2epub.model import Block, Book, Chapter, Kind
from pdf2epub.web.server import create_app, main


def _book() -> Book:
    def chapter(title, page, text):
        blocks = [Block(Kind.HEADING, text=title, page=page, first_on_page=True), Block(Kind.PARAGRAPH, text=text, page=page)]
        return Chapter(title=title, blocks=blocks, in_toc=True)

    return Book(
        title="Book",
        authors=[],
        language="en",
        direction="ltr",
        chapters=[
            Chapter(title=None, blocks=[Block(Kind.PARAGRAPH, text="Copyright", page=1)], in_toc=False),
            chapter("One", 2, "First chapter."),
            chapter("Business card", 3, "Still the first chapter."),
            chapter("Two", 4, "Second chapter."),
        ],
    )


def test_apply_edits_merge_retitle_drop():
    book = _book()
    edits = Edits(
        title="New title",
        authors=["A. Writer"],
        language="he",
        chapters=[
            ChapterEdit(index=0, drop=True),
            ChapterEdit(index=1, title="Chapter 1"),
            ChapterEdit(index=2, merge=True),
        ],
    )
    out = apply_edits(book, edits)
    assert [c.title for c in out.chapters] == ["Chapter 1", "Two"]
    assert out.chapters[0].blocks[0].text == "Chapter 1"
    merged_heading = out.chapters[0].blocks[2]
    assert merged_heading.text == "Business card" and merged_heading.level >= 2
    assert (out.title, out.authors, out.language, out.direction) == ("New title", ["A. Writer"], "he", "rtl")
    assert len(book.chapters) == 4 and book.chapters[1].title == "One"  # the original is untouched


def test_edits_from_json_and_summaries():
    edits = Edits.from_json({"title": " T ", "authors": "A, B", "chapters": [{"index": 1, "in_toc": False, "bogus": 1}]})
    assert edits.title == "T" and edits.authors == ["A", "B"] and edits.chapters[0].in_toc is False
    summaries = chapter_summaries(_book())
    assert summaries[1]["title"] == "One" and summaries[1]["first_page"] == 2 and summaries[1]["words"] == 3


def test_reserved_and_busy_ports_refused(capsys):
    assert main(["--port", "8000", "--no-browser"]) == 2
    assert main(["--port", "3002", "--no-browser"]) == 2
    assert "left free" in capsys.readouterr().err


def test_upload_rejects_non_pdf(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    res = client.post("/api/jobs", files={"file": ("notes.pdf", b"hello", "application/pdf")})
    assert res.status_code == 400
    assert client.get("/api/jobs").json() == []
    assert client.get("/").status_code == 200


@pytest.mark.slow
def test_upload_review_build_download(tmp_path: Path):
    from test_end_to_end import LOREM, _make_pdf

    pdf = tmp_path / "in.pdf"
    _make_pdf(pdf, "Web Book", [("Chapter One", [LOREM, LOREM]), ("Chapter Two", [LOREM])], rtl=False)
    client = TestClient(create_app(tmp_path / "data"))

    with open(pdf, "rb") as fh:
        job = client.post("/api/jobs", files={"file": ("Web_Book.pdf", fh, "application/pdf")}, data={"cover": "false"}).json()

    def wait(status: str) -> dict:
        deadline = time.time() + 300
        while time.time() < deadline:
            current = client.get(f"/api/jobs/{job['id']}").json()
            assert current["status"] != "failed", current["error"]
            if current["status"] == status:
                return current
            time.sleep(0.5)
        raise AssertionError(f"job never reached {status}")

    wait("review")
    draft = client.get(f"/api/jobs/{job['id']}/draft").json()
    titles = [c["title"] for c in draft["chapters"] if c["in_toc"]]
    assert titles == ["Chapter One", "Chapter Two"]
    first = next(c for c in draft["chapters"] if c["title"] == "Chapter One")
    preview = client.get(f"/api/jobs/{job['id']}/chapters/{first['index']}").json()
    assert any("thirteen" in b["text"] for b in preview["blocks"])

    edits = {"title": "Renamed", "chapters": [{"index": first["index"], "title": "The Start"}]}
    assert client.post(f"/api/jobs/{job['id']}/build", json=edits).status_code == 200
    done = wait("done")
    assert done["report"]["toc_entries"] == 2

    res = client.get(f"/api/jobs/{job['id']}/epub")
    assert res.status_code == 200 and res.headers["content-type"] == "application/epub+zip"
    assert "Renamed.epub" in res.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        assert "The Start" in z.read("EPUB/nav.xhtml").decode()

    assert client.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert client.get("/api/jobs").json() == []
