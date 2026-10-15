"""Local web UI: `pdf2epub-web` serves http://127.0.0.1:5050 by default.

Converts PDFs to EPUB and enriches existing EPUB files; the history is kept in data/pdf2epub.db.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import socket
import sys
import threading
import webbrowser
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import config
from ..edit import chapter_preview, chapter_summaries
from ..model import Kind
from ..validate import epubcheck_available
from .jobs import Job, JobStore

STATIC = Path(__file__).parent / "static"
DEFAULT_PORT = 5050  # 5000 is often taken (macOS AirPlay receiver, Docker Desktop)
# Common dev-server ports (Node 3000-3004, Django/FastAPI/uvicorn 8000-8020): never take them, so
# pdf2epub can run next to other local projects.
RESERVED_PORTS = set(range(3000, 3005)) | set(range(8000, 8021))
MAX_UPLOAD = 2 * 1024**3
MAX_COVER = 20 * 1024**2


def _kind_of(first_chunk: bytes, name: str) -> str | None:
    """'pdf' or 'epub' from the file's first bytes (an EPUB is a ZIP file)."""
    if first_chunk.startswith(b"%PDF"):
        return "pdf"
    if first_chunk.startswith(b"PK\x03\x04") and (b"application/epub+zip" in first_chunk[:200] or name.lower().endswith(".epub")):
        return "epub"
    return None


def _is_epub(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return "META-INF/container.xml" in z.namelist()
    except zipfile.BadZipFile:
        return False


def create_app(data_dir: Path) -> FastAPI:
    app = FastAPI(title="pdf2epub", docs_url=None, redoc_url=None)
    store = JobStore(data_dir)
    app.state.store = store

    def job_or_404(job_id: str) -> Job:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, "No such job")
        return job

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/info")
    def info() -> dict:
        try:
            config.tesseract_cmd()
            tesseract = True
        except RuntimeError:
            tesseract = False
        return {"tesseract": tesseract, "epubcheck": epubcheck_available()}

    @app.get("/api/jobs")
    def list_jobs() -> list[dict]:
        return [j.public() for j in store.list()]

    @app.post("/api/jobs")
    async def create_job(
        file: UploadFile = File(...),
        title: str = Form(""),
        author: str = Form(""),
        language: str = Form(""),
        ocr: str = Form("auto"),
        ocr_lang: str = Form(""),
        cover: bool = Form(True),
        review: bool = Form(True),
    ) -> dict:
        name = Path(file.filename or "book.pdf").name
        if ocr not in ("auto", "always", "never"):
            raise HTTPException(400, "ocr must be auto, always or never")
        if ocr_lang and not re.fullmatch(r"[a-z_]{3,8}(\+[a-z_]{3,8})*", ocr_lang):
            raise HTTPException(400, "OCR language must look like heb, eng or heb+eng")
        first = await file.read(1024 * 1024)
        if not first:
            raise HTTPException(400, "The file is empty")
        kind = _kind_of(first, name)
        if kind is None:
            raise HTTPException(400, f"{name} is neither a PDF nor an EPUB file")
        options = {"title": title.strip(), "author": author.strip(), "language": language.strip(),
                   "ocr": ocr, "ocr_lang": ocr_lang.strip(), "cover": cover}
        job = store.create(name, options, review, kind)
        target = store.input_path(job)
        size = 0
        with open(target, "wb") as fh:
            chunk = first
            while chunk:
                size += len(chunk)
                if size > MAX_UPLOAD:
                    fh.close()
                    store.discard(job)
                    raise HTTPException(413, "File is larger than 2 GB")
                fh.write(chunk)
                chunk = await file.read(1024 * 1024)
        if kind == "epub" and not _is_epub(target):
            store.discard(job)
            raise HTTPException(400, f"{name} is not a valid EPUB file")
        store.submit(job)
        return job.public()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        return job_or_404(job_id).public()

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str) -> Response:
        try:
            store.delete(job_or_404(job_id))
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(status_code=204)

    def prepared(job: Job) -> None:
        if job.summary is None or (job.kind == "pdf" and not (store.dir(job.id) / "draft.pkl").is_file()):
            raise HTTPException(409, "This job has not been prepared yet")

    def draft_of(job: Job):
        if job.kind != "pdf":
            raise HTTPException(409, "Only PDF conversions have chapters to review")
        prepared(job)
        return store.draft(job)

    @app.get("/api/jobs/{job_id}/draft")
    def get_draft(job_id: str) -> dict:
        job = job_or_404(job_id)
        if job.kind == "epub":
            prepared(job)
            return {
                "kind": "epub",
                "summary": job.summary,
                "edits": job.edits,
                "has_cover": store.epub_cover(job) is not None,
                "new_cover": store.cover_upload_path(job).is_file(),
            }
        book = draft_of(job).book
        return {
            "kind": "pdf",
            "title": book.title,
            "authors": book.authors,
            "language": book.language,
            "direction": book.direction,
            "has_cover": book.cover is not None,
            "chapters": chapter_summaries(book),
            "summary": job.summary,
            "edits": job.edits,
        }

    @app.get("/api/jobs/{job_id}/chapters/{index}")
    def get_chapter(job_id: str, index: int) -> dict:
        book = draft_of(job_or_404(job_id)).book
        if not 0 <= index < len(book.chapters):
            raise HTTPException(404, "No such chapter")
        return {"direction": book.direction, "blocks": chapter_preview(book, index)}

    @app.get("/api/jobs/{job_id}/chapters/{index}/images/{n}")
    def get_image(job_id: str, index: int, n: int) -> Response:
        book = draft_of(job_or_404(job_id)).book
        try:
            block = book.chapters[index].blocks[n]
        except IndexError:
            raise HTTPException(404, "No such image") from None
        if block.kind is not Kind.IMAGE or not block.image:
            raise HTTPException(404, "No such image")
        return Response(block.image, media_type="image/png" if block.image_ext == "png" else "image/jpeg")

    @app.get("/api/jobs/{job_id}/cover")
    def get_cover(job_id: str) -> Response:
        job = job_or_404(job_id)
        cover = store.epub_cover(job) if job.kind == "epub" else draft_of(job).book.cover
        if not cover:
            raise HTTPException(404, "No cover")
        media = "image/png" if cover[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        return Response(cover, media_type=media, headers={"Cache-Control": "no-cache"})

    @app.post("/api/jobs/{job_id}/cover")
    async def upload_cover(job_id: str, file: UploadFile = File(...)) -> dict:
        """A new cover image for an EPUB that is being enriched."""
        job = job_or_404(job_id)
        if job.kind != "epub":
            raise HTTPException(409, "Covers can be replaced for EPUB files only")
        data = await file.read(MAX_COVER + 1)
        if len(data) > MAX_COVER:
            raise HTTPException(413, "The image is larger than 20 MB")
        if not (data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n")):
            raise HTTPException(400, "The cover must be a JPEG or PNG image")
        store.cover_upload_path(job).write_bytes(data)
        return {"ok": True}

    @app.delete("/api/jobs/{job_id}/cover")
    def remove_cover_upload(job_id: str) -> Response:
        store.cover_upload_path(job_or_404(job_id)).unlink(missing_ok=True)
        return Response(status_code=204)

    @app.post("/api/jobs/{job_id}/build")
    def build_job(job_id: str, edits: dict) -> dict:
        job = job_or_404(job_id)
        if job.status in ("queued", "preparing", "building"):
            raise HTTPException(409, "The job is still running")
        if job.kind == "pdf":
            draft_of(job)
        else:
            prepared(job)
        store.request_build(job, edits)
        return job.public()

    @app.get("/api/jobs/{job_id}/epub")
    def download(job_id: str) -> FileResponse:
        job = job_or_404(job_id)
        path = store.epub_path(job)
        if job.status != "done" or not path.is_file():
            raise HTTPException(409, "The EPUB is not ready")
        name = re.sub(r'[\\/:*?"<>|]+', " ", job.title).strip() or "book"
        return FileResponse(path, media_type="application/epub+zip", filename=f"{name}.epub")

    @app.exception_handler(RuntimeError)
    async def runtime_error(_request, exc: RuntimeError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=500)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    p = argparse.ArgumentParser(prog="pdf2epub-web", description="Run the pdf2epub web UI locally.")
    p.add_argument("--host", default="127.0.0.1", help="interface to listen on (default: 127.0.0.1, this computer only)")
    p.add_argument("--port", type=int, default=int(os.environ.get("PDF2EPUB_PORT", DEFAULT_PORT)))
    p.add_argument("--data", type=Path, default=Path(os.environ.get("PDF2EPUB_DATA", config.PROJECT_ROOT / "data")))
    p.add_argument("--no-browser", action="store_true", help="don't open the browser")
    args = p.parse_args(argv)

    if args.port in RESERVED_PORTS:
        print(f"error: port {args.port} is left free for other dev servers (3000-3004, 8000-8020); pick another", file=sys.stderr)
        return 2
    if not _port_free(args.host, args.port):
        print(f"error: port {args.port} is already in use; run with --port <other>", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("pdf2epub").setLevel(logging.INFO)
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    app = create_app(args.data)
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{args.port}"
    print(f"pdf2epub is running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
