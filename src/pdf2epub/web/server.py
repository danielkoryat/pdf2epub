"""The web app: `pdf2epub-web` serves http://127.0.0.1:5050 by default.

Converts PDFs to EPUB, enriches existing EPUB files, and turns finished books into audiobooks (the voices are made
by a voice worker, see voice.py). In local mode it is a personal tool (no sign-in, the worker runs inside it, and
it starts this computer's voice worker when one is installed). In server mode (PDF2EPUB_MODE=server) it is a
public website: Google sign-in, every user sees only their own books, rate limits and page allowances, an admin
dashboard and an audit log; conversions run in the separate sandboxed `pdf2epub-worker` and the voices in the
voice container. See settings.py for the configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import config
from ..edit import chapter_preview, chapter_summaries
from ..model import Kind
from ..validate import epubcheck_available
from ..voices import catalog, estimate, measured_speeds, validate_options
from . import admin as admin_api
from . import voice as voice_api
from .auth import Auth
from .db import Database
from .jobs import ACTIVE, Job, Library
from .quota import Quota
from .security import RateLimiter, SecurityMiddleware, network_of
from .settings import Settings
from .uploads import UploadRejected, check_epub, check_image, safe_filename, sniff

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
DEFAULT_PORT = 5050  # 5000 is often taken (macOS AirPlay receiver, Docker Desktop)
# Common dev-server ports (Node 3000-3004, Django/FastAPI/uvicorn 8000-8020): never take them in local mode, so
# pdf2epub can run next to other local projects.
RESERVED_PORTS = set(range(3000, 3005)) | set(range(8000, 8021))
CHUNK = 1024 * 1024


def page(name: str) -> str:
    """A page from static/, with ?v=<content hash> on its /static/ links. Cloudflare keeps scripts and styles
    in browsers for hours whatever the server says; a new version gets a new address, so it is picked up at once."""
    html = (STATIC / name).read_text(encoding="utf-8")

    def versioned(match: re.Match) -> str:
        target = STATIC / match.group(2)
        if not target.is_file():
            return match.group(0)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()[:12]
        return f'{match.group(1)}/static/{match.group(2)}?v={digest}"'

    return re.sub(r'((?:src|href)=")/static/([\w.-]+)"', versioned, html)


class GuestBody(BaseModel):
    turnstile: str = Field(default="", max_length=2048)  # the Cloudflare Turnstile token, when the check is on


class AudiobookBody(BaseModel):
    engine: str = Field(max_length=20)
    voice: str = Field(max_length=40)
    speed: float = 1.0
    quality: str = Field(default="", max_length=20)


def create_app(data_dir: Path | None = None, settings: Settings | None = None, start_worker: bool | None = None) -> FastAPI:
    if settings is None:
        settings = Settings.from_env(**({"data_dir": Path(data_dir)} if data_dir is not None else {}))
    db = Database(settings.data_dir / "pdf2epub.db")
    library = Library(settings.data_dir, db, allow_pickle=not settings.server)
    limiter = RateLimiter()
    auth = Auth(settings, db, limiter)
    quota = Quota(db, settings.limits)
    limits = settings.limits

    app = FastAPI(title="pdf2epub", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.db, app.state.library = settings, db, library
    app.add_middleware(SecurityMiddleware, settings=settings)
    if settings.server:
        from starlette.middleware.sessions import SessionMiddleware

        # Only for the short-lived OAuth state/nonce during sign-in; the real session is our own cookie.
        app.add_middleware(SessionMiddleware, secret_key=settings.effective_session_secret(), session_cookie="pdf2epub_oauth",
                           max_age=600, same_site="lax", https_only=settings.secure_cookies)

    if start_worker if start_worker is not None else settings.worker_in_process:
        from .worker import Worker

        worker = Worker(settings, db, library, isolate=False, name="in-process")
        threading.Thread(target=worker.run, name="pdf2epub-worker", daemon=True).start()
        app.state.worker = worker

    # -- dependencies --------------------------------------------------------------------------------------

    def limited(request: Request, user: dict, kind: str) -> None:
        if not settings.server:
            return
        ok, wait = limiter.allow(kind, user["id"])
        if not ok:
            auth.audit(request, user, "rate.limited", "denied", target=kind)
            raise HTTPException(429, f"Too many requests, please wait {wait} seconds", headers={"Retry-After": str(wait)})

    def reader(request: Request) -> dict:
        user = auth.user(request)
        limited(request, user, "read")
        return user

    def writer(request: Request) -> dict:
        user = auth.user(request)
        limited(request, user, "write")
        return user

    def owned(job_id: str, user: dict) -> Job:
        """The user's own job; anything else (other people's books included) is 'not found'."""
        job = library.get(job_id, owner_id=user["id"] if settings.server else None)
        if job is None:
            raise HTTPException(404, "No such book")
        return job

    def public(job: Job) -> dict:
        data = job.public(library.queue_position(job))
        if job.kind == "audio" and job.status == "building" and job.pending == "speak" and job.total:
            # What is left to read, at the speed the reading worker measured (a laptop's GPU is many times
            # faster than the server; the estimate made when the book was queued did not know which would read it).
            reading = [w for w in db.voice_workers() if w["name"] == job.worker] or voice_api.online_workers(db)
            data["seconds_left"] = estimate(max(job.total - job.done, 0), job.options, measured_speeds(reading))["making_seconds"]
        return data

    # -- pages and sign-in ---------------------------------------------------------------------------------

    @app.get("/")
    def index() -> HTMLResponse:
        return HTMLResponse(page("index.html"), headers={"Cache-Control": "no-cache"})

    @app.get("/privacy")
    def privacy() -> HTMLResponse:
        return HTMLResponse(page("privacy.html"), headers={"Cache-Control": "no-cache"})

    @app.get("/terms")
    def terms() -> HTMLResponse:
        return HTMLResponse(page("terms.html"), headers={"Cache-Control": "no-cache"})

    @app.get("/auth/login")
    async def login(request: Request) -> Response:
        if not settings.server:
            return RedirectResponse("/")
        return await auth.login(request)

    @app.get("/auth/callback")
    async def callback(request: Request) -> Response:
        if not settings.server:
            return RedirectResponse("/")
        return await auth.callback(request)

    @app.post("/auth/guest")
    async def guest(request: Request, body: GuestBody) -> Response:
        """Use the site without signing in (server mode)."""
        if not settings.server:
            return Response(status_code=204)
        return await auth.start_guest(request, body.turnstile)

    @app.post("/auth/logout")
    def logout(request: Request) -> Response:
        return auth.logout(request)

    @app.get("/api/info")
    def info() -> dict:
        try:
            config.tesseract_cmd()
            tesseract = True
        except RuntimeError:
            tesseract = False
        data: dict[str, Any] = {"mode": settings.mode, "tesseract": tesseract, "epubcheck": epubcheck_available()}
        if settings.server:
            data["guests"] = settings.allow_guests
            data["turnstile"] = settings.turnstile_site_key if settings.turnstile_secret else ""
        if settings.posthog_key:
            data["posthog"] = {"key": settings.posthog_key, "host": settings.posthog_host}
        return data

    def network(request: Request) -> str:
        return network_of(auth.client(request).ip)

    def limits_for(user: dict) -> dict[str, int] | None:
        """The upload limits that apply to this user; None for admins, who have none (besides the file size)."""
        if user["role"] == "admin":
            return None
        guest = user["role"] == "guest"
        return {
            "max_upload_mb": limits.max_upload_mb, "max_pdf_pages": limits.max_pdf_pages,
            "max_jobs_per_day": limits.guest_jobs_per_day if guest else limits.max_jobs_per_day,
            "max_waiting_jobs": limits.max_waiting_jobs,
            "max_storage_mb": limits.guest_max_storage_mb if guest else limits.max_storage_mb,
        }

    def audiobooks_for(user: dict) -> bool:
        """Who may ask for audiobooks: nobody, admins, or everyone signed in with Google (guests never: the voices
        take hours of a home computer's time)."""
        if settings.audiobooks == "off":
            return False
        if settings.audiobooks == "admins":
            return user["role"] == "admin"
        return user["role"] in ("user", "admin")

    @app.get("/api/me")
    def me(request: Request, user: dict = Depends(reader)) -> dict:
        data = {k: user[k] for k in ("id", "email", "name", "picture", "role")}
        data["audiobooks"] = audiobooks_for(user)
        if settings.server:
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            data["balance"] = quota.balance(user["id"], network(request)).as_dict()
            data["usage"] = {
                "storage_bytes": library.storage_used(user["id"]),
                "books_today": library.jobs_since(user["id"], today),
                "waiting": library.waiting(user["id"]),
            }
            data["limits"] = limits_for(user)
        return data

    # -- the library ---------------------------------------------------------------------------------------

    @app.get("/api/jobs")
    def list_jobs(user: dict = Depends(reader)) -> list[dict]:
        return [public(j) for j in library.list(user["id"] if settings.server else None)]

    @app.post("/api/jobs")
    async def create_job(
        request: Request,
        file: UploadFile = File(...),
        title: str = Form("", max_length=300),
        author: str = Form("", max_length=300),
        language: str = Form("", max_length=20),
        ocr: str = Form("auto"),
        ocr_lang: str = Form("", max_length=40),
        cover: bool = Form(True),
        review: bool = Form(True),
        user: dict = Depends(auth.user),
    ) -> dict:
        name = safe_filename(file.filename or "book")

        def reject(status: int, message: str, reason: str) -> HTTPException:
            auth.audit(request, user, "job.upload", "denied", target=name, reason=reason)
            return HTTPException(status, message)

        limited(request, user, "upload")
        if ocr not in ("auto", "always", "never"):
            raise HTTPException(400, "ocr must be auto, always or never")
        if ocr_lang and not re.fullmatch(r"[a-z_]{3,8}(\+[a-z_]{3,8})*", ocr_lang):
            raise HTTPException(400, "OCR language must look like heb, eng or heb+eng")
        if language and not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*", language):
            raise HTTPException(400, "Language must be a code like he, en or pt-BR")
        max_bytes = (limits.max_upload_mb if settings.server else 2048) * CHUNK
        net = network(request) if settings.server else ""
        own = limits_for(user) if settings.server else None
        if own is not None:
            guest = user["role"] == "guest"
            more = " Sign in with Google for more." if guest else ""
            if library.waiting(user["id"]) >= own["max_waiting_jobs"]:
                raise reject(429, f"You already have {own['max_waiting_jobs']} books waiting or converting. Please wait for them to finish.", "waiting")
            since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            today_count = library.guest_jobs_since(net, since) if guest else library.jobs_since(user["id"], since)
            if today_count >= own["max_jobs_per_day"]:
                raise reject(429, f"You reached today's limit of {own['max_jobs_per_day']} books. Please come back tomorrow.{more}", "daily")
            room = own["max_storage_mb"] * CHUNK - library.storage_used(user["id"])
            if room <= 0:
                raise reject(413, f"Your storage is full. Delete some books to make room.{more}", "storage")
            max_bytes = min(max_bytes, room)
        first = await file.read(CHUNK)
        if not first:
            raise reject(400, "The file is empty", "empty")
        try:
            kind = sniff(first, name)
        except UploadRejected as exc:
            raise reject(400, str(exc), "type") from None
        if settings.server and kind == "pdf" and quota.balance(user["id"], net).available <= 0:
            message = ("You have used this month's free pages for guests. Sign in with Google to get more."
                       if user["role"] == "guest" else "You have no pages left this month. More pages will be available to buy soon.")
            raise reject(402, message, "quota")
        options = {"title": title.strip(), "author": author.strip(), "language": language.strip(),
                   "ocr": ocr, "ocr_lang": ocr_lang.strip(), "cover": cover}
        job = library.create(user["id"], name, options, review, kind, ip=net)
        size = 0
        try:
            with open(library.input_path(job), "wb") as fh:
                chunk = first
                while chunk:
                    size += len(chunk)
                    if size > max_bytes:
                        raise UploadRejected(f"The file is larger than {max_bytes // CHUNK} MB" if size > limits.max_upload_mb * CHUNK or not settings.server
                                             else "The file doesn't fit in your remaining storage")
                    fh.write(chunk)
                    chunk = await file.read(CHUNK)
            if kind == "epub":
                check_epub(library.input_path(job), limits.max_epub_entries, limits.max_epub_unpacked_mb)
        except UploadRejected as exc:
            library.discard(job)
            raise reject(413 if "larger" in str(exc) or "storage" in str(exc) else 400, str(exc), "content") from None
        library.submit(job)
        auth.audit(request, user, "job.upload", target=job.id, kind=kind, bytes=size, filename=name)
        return public(job)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, user: dict = Depends(reader)) -> dict:
        return public(owned(job_id, user))

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str, request: Request, user: dict = Depends(writer)) -> Response:
        job = owned(job_id, user)
        try:
            library.delete(job)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        auth.audit(request, user, "job.delete", target=job.id)
        return Response(status_code=204)

    def prepared(job: Job) -> None:
        if job.summary is None or (job.kind == "pdf" and not library.has_draft(job)):
            raise HTTPException(409, "This book has not been prepared yet")

    def draft_of(job: Job):
        if job.kind != "pdf":
            raise HTTPException(409, "Only PDF conversions have chapters to review")
        prepared(job)
        return library.draft(job)

    @app.get("/api/jobs/{job_id}/draft")
    def get_draft(job_id: str, user: dict = Depends(reader)) -> dict:
        job = owned(job_id, user)
        if job.kind == "epub":
            prepared(job)
            return {
                "kind": "epub",
                "summary": job.summary,
                "edits": job.edits,
                "has_cover": library.epub_cover(job) is not None,
                "new_cover": library.cover_upload_path(job).is_file(),
            }
        book = draft_of(job).book
        new_cover = library.cover_upload_path(job).is_file()
        return {
            "kind": "pdf",
            "title": book.title,
            "authors": book.authors,
            "language": book.language,
            "direction": book.direction,
            "has_cover": book.cover is not None or new_cover,
            "new_cover": new_cover,
            "chapters": chapter_summaries(book),
            "summary": job.summary,
            "edits": job.edits,
        }

    @app.get("/api/jobs/{job_id}/chapters/{index}")
    def get_chapter(job_id: str, index: int, user: dict = Depends(reader)) -> dict:
        book = draft_of(owned(job_id, user)).book
        if not 0 <= index < len(book.chapters):
            raise HTTPException(404, "No such chapter")
        return {"direction": book.direction, "blocks": chapter_preview(book, index)}

    @app.get("/api/jobs/{job_id}/chapters/{index}/images/{n}")
    def get_image(job_id: str, index: int, n: int, user: dict = Depends(reader)) -> Response:
        book = draft_of(owned(job_id, user)).book
        try:
            block = book.chapters[index].blocks[n]
        except IndexError:
            raise HTTPException(404, "No such image") from None
        if block.kind is not Kind.IMAGE or not block.image:
            raise HTTPException(404, "No such image")
        return Response(block.image, media_type="image/png" if block.image_ext == "png" else "image/jpeg")

    @app.get("/api/jobs/{job_id}/cover")
    def get_cover(job_id: str, user: dict = Depends(reader)) -> Response:
        job = owned(job_id, user)
        upload = library.cover_upload_path(job)
        if job.kind == "epub":
            cover = library.epub_cover(job)
        else:
            cover = upload.read_bytes() if upload.is_file() else draft_of(job).book.cover
        if not cover:
            raise HTTPException(404, "No cover")
        try:
            media = check_image(cover, max_bytes=50 * CHUNK)
        except UploadRejected:
            raise HTTPException(404, "No cover") from None
        return Response(cover, media_type=media, headers={"Cache-Control": "private, no-cache"})

    @app.post("/api/jobs/{job_id}/cover")
    async def upload_cover(job_id: str, request: Request, file: UploadFile = File(...), user: dict = Depends(writer)) -> dict:
        """A new cover image for a book that is being built or enriched."""
        job = owned(job_id, user)
        if job.kind == "pdf":
            draft_of(job)
        data = await file.read(20 * CHUNK + 1)
        try:
            check_image(data)
        except UploadRejected as exc:
            raise HTTPException(400, str(exc)) from None
        library.cover_upload_path(job).write_bytes(data)
        auth.audit(request, user, "job.cover", target=job.id, bytes=len(data))
        return {"ok": True}

    @app.delete("/api/jobs/{job_id}/cover")
    def remove_cover_upload(job_id: str, user: dict = Depends(writer)) -> Response:
        library.cover_upload_path(owned(job_id, user)).unlink(missing_ok=True)
        return Response(status_code=204)

    @app.post("/api/jobs/{job_id}/build")
    def build_job(job_id: str, edits: dict, request: Request, user: dict = Depends(writer)) -> dict:
        job = owned(job_id, user)
        if job.status in ACTIVE:
            raise HTTPException(409, "The book is still being processed")
        if len(str(edits)) > 2_000_000:
            raise HTTPException(413, "Too many edits")
        if job.kind == "pdf":
            draft_of(job)
        else:
            prepared(job)
        library.request_build(job, edits)
        auth.audit(request, user, "job.build", target=job.id)
        return public(library.get(job.id))

    @app.get("/api/voices")
    def voices(user: dict = Depends(reader)) -> dict:
        """The voices an audiobook can be read with, and whether a voice worker is online to read it."""
        if not audiobooks_for(user):
            raise HTTPException(404, "Not found")
        workers = voice_api.online_workers(db)
        return {**catalog(measured_speeds(workers)), "online": bool(workers), "online_engines": sorted({e for w in workers for e in w["engines"]})}

    @app.post("/api/jobs/{job_id}/audiobook")
    def make_audiobook(job_id: str, body: AudiobookBody, request: Request, user: dict = Depends(writer)) -> dict:
        """Read a finished book aloud: a new audio job, with a copy of the book's EPUB."""
        if not audiobooks_for(user):
            raise HTTPException(404, "Not found")
        source = owned(job_id, user)
        if source.kind not in ("pdf", "epub") or source.status != "done" or not library.epub_path(source).is_file():
            raise HTTPException(409, "The book is not ready yet")
        try:
            options = validate_options(body.model_dump(), source.language)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        workers = voice_api.online_workers(db)
        if workers and not any(options["engine"] in w["engines"] for w in workers):
            raise HTTPException(400, "Those voices can't be made here. Please choose another voice engine.")
        if settings.server and user["role"] != "admin":
            since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            if library.jobs_since(user["id"], since, kind="audio") >= limits.audiobooks_per_day:
                raise HTTPException(429, f"You reached today's limit of {limits.audiobooks_per_day} audiobooks. Please come back tomorrow.")
            if library.waiting(user["id"]) >= limits.max_waiting_jobs:
                raise HTTPException(429, f"You already have {limits.max_waiting_jobs} books waiting or in progress. Please wait for them to finish.")
            if library.storage_used(user["id"]) >= limits.max_storage_mb * CHUNK:
                raise HTTPException(413, "Your storage is full. Delete some books to make room.")
        job = library.create(user["id"], f"{source.title}.m4b", {**options, "source": source.id}, review=False, kind="audio",
                             ip=network(request) if settings.server else "")
        job.title, job.authors, job.language = source.title, source.authors, options["language"]
        shutil.copyfile(library.epub_path(source), library.input_path(job))
        library.submit(job)
        auth.audit(request, user, "job.audiobook", target=job.id, source=source.id, engine=options["engine"], voice=options["voice"])
        return public(job)

    @app.get("/api/jobs/{job_id}/audiobook")
    def download_audiobook(job_id: str, request: Request, user: dict = Depends(auth.user)) -> FileResponse:
        limited(request, user, "download")
        job = owned(job_id, user)
        path = library.m4b_path(job)
        if job.kind != "audio" or job.status != "done" or not path.is_file():
            raise HTTPException(409, "The audiobook is not ready")
        auth.audit(request, user, "job.download", target=job.id, kind="audio")
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", job.title).strip()[:150] or "book"
        return FileResponse(path, media_type="audio/mp4", filename=f"{name}.m4b")

    @app.get("/api/jobs/{job_id}/epub")
    def download(job_id: str, request: Request, user: dict = Depends(auth.user)) -> FileResponse:
        limited(request, user, "download")
        job = owned(job_id, user)
        path = library.epub_path(job)
        if job.status != "done" or not path.is_file():
            raise HTTPException(409, "The EPUB is not ready")
        auth.audit(request, user, "job.download", target=job.id)
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", job.title).strip()[:150] or "book"
        return FileResponse(path, media_type="application/epub+zip", filename=f"{name}.epub")

    if settings.server:
        app.include_router(admin_api.router(settings, db, library, auth, quota))
    app.include_router(voice_api.router(settings, db, library, auth, limiter))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        request_id = getattr(request.state, "request_id", "")
        return JSONResponse({"detail": f"Something went wrong (reference {request_id})"}, status_code=500)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def _is_pdf2epub(url: str) -> bool:
    """Whether the server answering at url is pdf2epub (its /api/info reports the tools it found)."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"{url}/api/info", timeout=2) as res:
            return "epubcheck" in json.load(res)
    except (OSError, ValueError):
        return False


def _open_when_ready(url: str, timeout: float = 60.0) -> None:
    """Open the browser once the server answers, rather than after a fixed delay."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_pdf2epub(url):
            webbrowser.open(url)
            return
        time.sleep(0.3)


def _voice_worker_python() -> Path | None:
    """The voice worker's Python (voice-worker/.venv in this checkout), if it is installed."""
    venv = config.PROJECT_ROOT / "voice-worker" / ".venv"
    python = venv / "Scripts" / "python.exe" if os.name == "nt" else venv / "bin" / "python"
    return python if python.is_file() else None


_voice_worker: subprocess.Popen | None = None  # kept, so its standard input stays open while the app runs
VOICE_WORKER_STALLED = 3  # the worker's exit code when its voice engine got stuck (pdf2epub_voice.reader.EXIT_STALLED)


def _start_voice_worker(python: Path, url: str) -> None:
    """Local mode: make the audiobook voices on this computer while the app runs. The worker stops when its
    standard input closes, which happens when this process ends in any way. It starts once the app answers, so
    its first look for work does not fail, and again whenever it stops because its voice engine got stuck (it
    then continues the book from its finished chapters)."""
    import time

    global _voice_worker

    deadline = time.monotonic() + 60
    while not _is_pdf2epub(url):
        if time.monotonic() > deadline:
            return
        time.sleep(0.5)
    while True:
        _voice_worker = subprocess.Popen([str(python), "-X", "utf8", "-m", "pdf2epub_voice", "run", "--url", url, "--with-app"],
                                         cwd=python.parents[2], stdin=subprocess.PIPE)
        print("The voice worker is making audiobook voices on this computer")
        if _voice_worker.wait() != VOICE_WORKER_STALLED:
            return
        print("The voice worker got stuck; starting it again")


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    p = argparse.ArgumentParser(prog="pdf2epub-web", description="Run the pdf2epub web app.")
    p.add_argument("--host", default=os.environ.get("PDF2EPUB_HOST", "127.0.0.1"), help="interface to listen on (default: 127.0.0.1, this computer only)")
    p.add_argument("--port", type=int, default=int(os.environ.get("PDF2EPUB_PORT", DEFAULT_PORT)))
    p.add_argument("--data", type=Path, default=None, help="data folder (default: PDF2EPUB_DATA or ./data)")
    p.add_argument("--no-browser", action="store_true", help="don't open the browser")
    p.add_argument("--no-voice-worker", action="store_true",
                   help="local mode: don't start this computer's voice worker (voice-worker/.venv) for audiobooks")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("pdf2epub").setLevel(logging.INFO)
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{args.port}"
    overrides: dict[str, Any] = {"data_dir": args.data} if args.data else {}
    if os.environ.get("PDF2EPUB_MODE", "local").strip().lower() != "server":
        overrides["base_url"] = url  # local mode: the address it actually listens on
    settings = Settings.from_env(**overrides)
    if not settings.server:
        if args.port in RESERVED_PORTS:
            print(f"error: port {args.port} is left free for other dev servers (3000-3004, 8000-8020); pick another", file=sys.stderr)
            return 2
        if not _port_free(args.host, args.port):
            if _is_pdf2epub(url):
                # Started again (e.g. from the desktop shortcut) while already running: just show the page.
                print(f"pdf2epub is already running at {url}")
                if not args.no_browser:
                    webbrowser.open(url)
                return 0
            print(f"error: port {args.port} is already in use; run with --port <other>", file=sys.stderr)
            return 2

    app = create_app(settings=settings)
    print(f"pdf2epub ({settings.mode} mode) is running at {settings.base_url if settings.server else url}  (close this window or press Ctrl+C to stop)")
    if not args.no_browser and not settings.server:
        threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()
    if not settings.server and not args.no_voice_worker and (python := _voice_worker_python()):
        threading.Thread(target=_start_voice_worker, args=(python, url), daemon=True).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", proxy_headers=False, server_header=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
