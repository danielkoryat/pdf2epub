"""The voice worker's API (/api/voice): how audiobooks get their voices.

Making voices needs a GPU, which the server doesn't have. A voice worker (voice-worker/ in this repository) runs
on a computer that has one, anywhere: it connects out to the site, so nothing at its end has to be reachable.
It takes an audiobook job that is waiting for voices, downloads the text to read (script.json, made by the
server's sandboxed worker) and the cover, reports progress while it reads (which also serves as the job's
heartbeat), uploads the finished M4B in parts (Cloudflare refuses request bodies over 100 MB) and finishes.

Every request carries `Authorization: Bearer <PDF2EPUB_VOICE_TOKEN>`. Without a token configured the API is
off on a public server; locally it then answers this computer only. The worker names itself in the
X-Voice-Worker header, and only the worker that claimed a job may report on it.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import shutil
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from ..voices import ENGINES
from .auth import Auth
from .db import Database
from .jobs import Job, Library
from .security import RateLimiter
from .settings import Settings

PART_BYTES = 50 * 1024 * 1024
MAX_PARTS = 60  # 3 GB at most
ONLINE_SECONDS = 120  # a worker seen this recently counts as online


class ClaimBody(BaseModel):
    engines: list[str] = Field(default_factory=list, max_length=10)
    info: dict[str, Any] = Field(default_factory=dict)


class ProgressBody(BaseModel):
    done: int = Field(ge=0)
    total: int = Field(ge=0)
    log: list[str] = Field(default_factory=list, max_length=50)


class FinishBody(BaseModel):
    parts: int = Field(ge=1, le=MAX_PARTS)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seconds: float = Field(ge=0)
    chapters: int = Field(ge=0)


class FailBody(BaseModel):
    message: str = Field(max_length=500)


def online_workers(db: Database) -> list[dict[str, Any]]:
    cutoff = time.time() - ONLINE_SECONDS
    return [w for w in db.voice_workers() if w["last_seen"] >= cutoff]


def router(settings: Settings, db: Database, library: Library, auth: Auth, limiter: RateLimiter) -> APIRouter:
    r = APIRouter(prefix="/api/voice")

    def worker(request: Request) -> str:
        """The calling voice worker's name, once its token checks out."""
        token = settings.voice_token
        if not token:
            host = request.client.host if request.client else ""
            if settings.server or not _loopback(host):
                raise HTTPException(404, "Not found")
        else:
            header = request.headers.get("authorization", "")
            given = header[7:].strip() if header.lower().startswith("bearer ") else ""
            if not hmac.compare_digest(given.encode(), token.encode()):
                ip = auth.client(request).ip
                limiter.allow("auth", ip)
                auth.audit(request, None, "voice.denied", "denied", target=request.url.path)
                raise HTTPException(401, "Bad voice worker token")
        name = request.headers.get("x-voice-worker", "").strip()[:80]
        return f"voice:{name or 'worker'}"

    def claimed(job_id: str, name: str) -> Job:
        job = library.get(job_id)
        if job is None or job.kind != "audio":
            raise HTTPException(404, "No such job")  # deleted by its owner: the worker stops
        if job.pending != "speak" or job.status != "building" or job.worker != name:
            raise HTTPException(409, "This job is not being read by this worker")
        return job

    @r.post("/claim")
    def claim(body: ClaimBody, request: Request, name: str = Depends(worker)) -> Response:
        engines = [e for e in body.engines if e in ENGINES]
        db.voice_worker_seen(name, engines, body.info)
        row = db.claim_job(name, kinds=("audio",), actions=("speak",), engines=tuple(engines))
        if row is None:
            return Response(status_code=204)
        job = Job.from_row(row)
        db.update_job(job.id, stage="speak", log=[*job.log, f"{time.strftime('%H:%M:%S')} Reading aloud on {name[6:]}"][-200:])
        auth.audit(request, None, "voice.claim", target=job.id, worker=name, attempt=job.attempts)
        data = {k: getattr(job, k) for k in ("id", "title", "authors", "language", "options", "summary", "attempts")}
        data["has_cover"] = library.audio_cover_path(job).is_file()
        return JSONResponse(data)

    @r.get("/jobs/{job_id}/script")
    def script(job_id: str, name: str = Depends(worker)) -> FileResponse:
        job = claimed(job_id, name)
        return FileResponse(library.script_path(job), media_type="application/json")

    @r.get("/jobs/{job_id}/cover")
    def cover(job_id: str, name: str = Depends(worker)) -> Response:
        path = library.audio_cover_path(claimed(job_id, name))
        if not path.is_file():
            raise HTTPException(404, "No cover")
        data = path.read_bytes()
        return Response(data, media_type="image/png" if data[:4] == b"\x89PNG" else "image/jpeg")

    @r.post("/jobs/{job_id}/progress")
    def progress(job_id: str, body: ProgressBody, name: str = Depends(worker)) -> dict:
        job = claimed(job_id, name)
        lines = [line[:300] for line in body.log]
        db.update_job(job.id, stage="speak", done=min(body.done, body.total), total=body.total, heartbeat=time.time(),
                      log=[*job.log, *lines][-200:])
        return {"ok": True}

    @r.put("/jobs/{job_id}/parts/{n}")
    async def part(job_id: str, n: int, request: Request, name: str = Depends(worker)) -> dict:
        job = claimed(job_id, name)
        if not 0 <= n < MAX_PARTS:
            raise HTTPException(400, "Bad part number")
        folder = library.parts_dir(job)
        folder.mkdir(exist_ok=True)
        size = 0
        tmp = folder / f"{n:03d}.tmp"
        with open(tmp, "wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > PART_BYTES:
                    fh.close()
                    tmp.unlink(missing_ok=True)
                    raise HTTPException(413, "Part too large")
                fh.write(chunk)
        tmp.replace(folder / f"{n:03d}.part")
        db.update_job(job.id, heartbeat=time.time())
        return {"ok": True, "bytes": size}

    @r.post("/jobs/{job_id}/finish")
    def finish(job_id: str, body: FinishBody, request: Request, name: str = Depends(worker)) -> dict:
        job = claimed(job_id, name)
        folder = library.parts_dir(job)
        parts = [folder / f"{i:03d}.part" for i in range(body.parts)]
        if not all(p.is_file() for p in parts):
            raise HTTPException(409, "Parts are missing")
        out = library.m4b_path(job)
        tmp = out.with_suffix(".tmp")
        digest = hashlib.sha256()
        with open(tmp, "wb") as fh:
            for p in parts:
                with open(p, "rb") as src:
                    while chunk := src.read(1024 * 1024):
                        digest.update(chunk)
                        fh.write(chunk)
        with open(tmp, "rb") as fh:
            head = fh.read(12)
        if digest.hexdigest() != body.sha256 or head[4:8] != b"ftyp":
            tmp.unlink(missing_ok=True)
            raise HTTPException(400, "The audiobook arrived damaged; please send it again")
        tmp.replace(out)
        shutil.rmtree(folder, ignore_errors=True)
        library.input_path(job).unlink(missing_ok=True)  # the copy of the book is no longer needed
        size = out.stat().st_size
        report = {"size": size, "seconds": round(body.seconds), "chapters": body.chapters, "worker": name[6:]}
        db.update_job(job.id, status="done", pending=None, stage="", report=report, size=library.folder_size(job.id),
                      log=[*job.log, f"{time.strftime('%H:%M:%S')} Audiobook ready: {size / 1024 / 1024:.0f} MB"][-200:])
        auth.audit(request, None, "voice.finish", target=job.id, worker=name, bytes=size)
        return {"ok": True}

    @r.post("/jobs/{job_id}/fail")
    def fail(job_id: str, body: FailBody, request: Request, name: str = Depends(worker)) -> dict:
        job = claimed(job_id, name)
        db.update_job(job.id, status="failed", pending=None, error=body.message.strip() or "Making the audiobook failed")
        shutil.rmtree(library.parts_dir(job), ignore_errors=True)
        auth.audit(request, None, "voice.fail", "error", target=job.id, worker=name, message=body.message[:200])
        return {"ok": True}

    return r


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "testclient"  # the test suite's client
