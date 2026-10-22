"""Jobs as the web app sees them: records in the database plus files in data/jobs/<id>/.

The web process never converts anything. It creates a job and marks what should happen next (`pending`:
prepare or build); a worker (worker.py) claims it from the queue, runs it, and reports progress back through
the database. Three kinds of job:
  * pdf:  convert a PDF. queued -> preparing (OCR, structure) -> review -> building -> done
  * epub: enrich an existing EPUB. queued -> preparing (inspection) -> review -> building -> done
  * audio: read a finished book aloud. queued -> preparing (the text to read) -> queued (pending "speak") ->
    building (a voice worker on another computer makes the voices, see voice.py) -> done
With review switched off the worker builds straight after preparing; a failure ends in "failed".

Files of a job: input.pdf or input.epub, draft.json (a prepared PDF), cover-original (an EPUB's cover, extracted
by the worker), cover-upload (a new cover the user chose), book.epub (the result). An audio job has input.epub
(a copy of the book it reads), script.json (the text to read), cover (the book's cover, JPEG or PNG), parts/
(the audiobook while it is uploaded in pieces) and book.m4b (the result).
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from ..draftio import load_draft
from ..pipeline import Draft
from .db import Database

log = logging.getLogger(__name__)

ACTIVE = ("queued", "preparing", "building")
STAGE_LABELS = {
    "analyse": "Analysing pages",
    "extract": "Reading pages",
    "structure": "Finding chapters",
    "write": "Writing EPUB",
    "inspect": "Inspecting the EPUB",
    "enrich": "Enriching the EPUB",
    "narrate": "Preparing the text to read",
    "speak": "Reading aloud",
}


@dataclass
class Job:
    id: str
    filename: str
    created: float
    options: dict
    kind: str = "pdf"  # pdf | epub | audio
    owner_id: str = "local"
    review: bool = True
    status: str = "queued"
    pending: str | None = None  # what a worker should do next: prepare | build | speak (audio)
    stage: str = ""
    done: int = 0
    total: int = 0
    error: str = ""
    log: list[str] = field(default_factory=list)
    summary: dict | None = None  # facts about the prepared draft / the inspected EPUB
    report: dict | None = None  # facts about the written EPUB
    edits: dict | None = None  # the last edits the EPUB was built with
    title: str = ""
    authors: str = ""
    language: str = ""
    worker: str = ""
    attempts: int = 0
    pages: int = 0
    size: int = 0
    speed: float | None = None  # audiobooks: how many times faster than real time it is being read
    ip: str = ""  # the network the upload came from (server mode; guests are counted by it)

    @classmethod
    def from_row(cls, row: dict) -> "Job":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in row.items() if k in known}
        data["log"] = data.get("log") or []
        return cls(**data)

    def public(self, queue_position: int = 0) -> dict:
        data = asdict(self)
        data["stage_label"] = STAGE_LABELS.get(self.stage, self.stage)
        data["log"] = self.log[-8:]
        data["queue_position"] = queue_position
        for private in ("worker", "attempts", "owner_id", "ip"):
            data.pop(private)
        return data

    def row(self) -> dict:
        data = asdict(self)
        data["log"] = self.log[-200:]
        return data


class Library:
    def __init__(self, data_dir: Path, db: Database, allow_pickle: bool = False):
        self.root = data_dir / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = db
        self.allow_pickle = allow_pickle  # legacy drafts, trusted only in local mode
        self._lock = threading.Lock()
        self._drafts: OrderedDict[str, tuple[float, Draft]] = OrderedDict()
        self._import_json_jobs()

    # -- files ---------------------------------------------------------------------------------------------

    def dir(self, job_id: str) -> Path:
        if not job_id.isalnum() or len(job_id) > 64:  # ids are hex; never let one build a path elsewhere
            raise ValueError("bad job id")
        return self.root / job_id

    def input_path(self, job: Job) -> Path:
        return self.dir(job.id) / f"input.{'epub' if job.kind == 'audio' else job.kind}"

    def cover_upload_path(self, job: Job) -> Path:
        return self.dir(job.id) / "cover-upload"

    def cover_original_path(self, job: Job) -> Path:
        return self.dir(job.id) / "cover-original"

    def draft_path(self, job: Job) -> Path:
        return self.dir(job.id) / "draft.json"

    def epub_path(self, job: Job) -> Path:
        return self.dir(job.id) / "book.epub"

    def script_path(self, job: Job) -> Path:
        return self.dir(job.id) / "script.json"

    def audio_cover_path(self, job: Job) -> Path:
        return self.dir(job.id) / "cover"

    def parts_dir(self, job: Job) -> Path:
        return self.dir(job.id) / "parts"

    def m4b_path(self, job: Job) -> Path:
        return self.dir(job.id) / "book.m4b"

    def has_draft(self, job: Job) -> bool:
        return self.draft_path(job).is_file() or (self.allow_pickle and (self.dir(job.id) / "draft.pkl").is_file())

    def folder_size(self, job_id: str) -> int:
        folder = self.dir(job_id)
        return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else 0

    # -- records -------------------------------------------------------------------------------------------

    def _import_json_jobs(self) -> None:
        """Import jobs recorded by the first versions as data/jobs/<id>/job.json files."""
        known = self.db.job_ids()
        for path in sorted(self.root.glob("*/job.json")):
            if path.parent.name in known:
                continue
            try:
                job = Job.from_row(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, TypeError, ValueError):
                log.warning("Skipping unreadable job %s", path.parent.name)
                continue
            if job.status in ACTIVE:
                job.status, job.error, job.pending = "failed", "Interrupted: the server stopped while this job was running.", None
            self.db.save_job(job.row())
            log.info("Imported job %s into the database", job.id)

    def get(self, job_id: str, owner_id: str | None = None) -> Job | None:
        """The job, or None when it doesn't exist or (with owner_id) belongs to someone else."""
        try:
            self.dir(job_id)
        except ValueError:
            return None
        row = self.db.job(job_id)
        if row is None or (owner_id is not None and row["owner_id"] != owner_id):
            return None
        return Job.from_row(row)

    def list(self, owner_id: str | None = None) -> list[Job]:
        return [Job.from_row(r) for r in self.db.jobs(owner_id)]

    def queue_position(self, job: Job) -> int:
        return self.db.queue_position(job.id) if job.status == "queued" else 0

    def create(self, owner_id: str, filename: str, options: dict, review: bool, kind: str, ip: str = "") -> Job:
        job = Job(id=uuid.uuid4().hex[:16], filename=filename, created=time.time(), options=options,
                  review=review, kind=kind, owner_id=owner_id, ip=ip)
        job.title = options.get("title") or Path(filename).stem.replace("_", " ")
        self.dir(job.id).mkdir(parents=True)
        return job

    def submit(self, job: Job) -> None:
        job.status, job.pending = "queued", "prepare"
        job.size = self.folder_size(job.id)
        self.db.save_job(job.row())

    def request_build(self, job: Job, edits: dict) -> None:
        self.db.update_job(job.id, status="queued", pending="build", error="", stage="", done=0, total=0, edits=edits)

    def discard(self, job: Job) -> None:
        """Remove a job that was created but never submitted (e.g. a rejected upload)."""
        shutil.rmtree(self.dir(job.id), ignore_errors=True)

    def delete(self, job: Job) -> None:
        # An audiobook waiting for voices or being read can go: the voice worker finds it gone and stops.
        if job.status in ACTIVE and job.pending != "speak":
            raise RuntimeError("A running job cannot be deleted.")
        with self._lock:
            self._drafts.pop(job.id, None)
        self.db.delete_job(job.id)
        shutil.rmtree(self.dir(job.id), ignore_errors=True)

    # -- per-user facts --------------------------------------------------------------------------------------

    def storage_used(self, owner_id: str) -> int:
        row = self.db.one("SELECT COALESCE(SUM(size), 0) AS n FROM jobs WHERE owner_id=?", (owner_id,))
        return int(row["n"]) if row else 0

    def jobs_since(self, owner_id: str, since: float, kind: str | None = None) -> int:
        """Jobs created since `since`: of one kind, or (kind=None) books uploaded, not counting audiobooks."""
        where = "kind=?" if kind else "kind!=?"
        return self.db.one(f"SELECT COUNT(*) AS n FROM jobs WHERE owner_id=? AND created>=? AND {where}", (owner_id, since, kind or "audio"))["n"]

    def guest_jobs_since(self, network: str, since: float) -> int:
        """Uploads by all guests from one network."""
        return self.db.one(
            "SELECT COUNT(*) AS n FROM jobs j JOIN users u ON u.id=j.owner_id WHERE u.role='guest' AND j.ip=? AND j.created>=?",
            (network, since),
        )["n"]

    def waiting(self, owner_id: str) -> int:
        return self.db.one(
            "SELECT COUNT(*) AS n FROM jobs WHERE owner_id=? AND status IN ('queued','preparing','building')", (owner_id,)
        )["n"]

    # -- drafts and covers ---------------------------------------------------------------------------------

    def draft(self, job: Job) -> Draft:
        """The prepared draft of a PDF job (cached: the review page asks for it repeatedly)."""
        path = self.draft_path(job)
        mtime = path.stat().st_mtime if path.is_file() else 0.0
        with self._lock:
            cached = self._drafts.get(job.id)
            if cached and cached[0] == mtime:
                self._drafts.move_to_end(job.id)
                return cached[1]
        if path.is_file():
            draft = load_draft(path)
        elif self.allow_pickle and (self.dir(job.id) / "draft.pkl").is_file():
            draft = _load_legacy_pickle(self.dir(job.id) / "draft.pkl")
        else:
            raise FileNotFoundError("no prepared draft")
        with self._lock:
            self._drafts[job.id] = (mtime, draft)
            while len(self._drafts) > 3:
                self._drafts.popitem(last=False)
        return draft

    def epub_cover(self, job: Job) -> bytes | None:
        """The cover of an EPUB job: a newly uploaded one, else the book's own (extracted by the worker)."""
        for path in (self.cover_upload_path(job), self.cover_original_path(job)):
            if path.is_file():
                return path.read_bytes()
        if self.allow_pickle:  # local mode, jobs prepared before covers were extracted: trusted input
            from ..enrich import EpubPackage

            pkg = EpubPackage.open(self.input_path(job))
            item = pkg.cover_item()
            return pkg.files.get(pkg.item_path(item)) if item is not None else None
        return None


def _load_legacy_pickle(path: Path) -> Draft:
    """Drafts saved by versions before draft.json (local mode only: the files were written on this computer)."""
    import dataclasses
    import pickle

    with open(path, "rb") as fh:
        draft = pickle.load(fh)  # noqa: S301 -- local, self-written files only (allow_pickle)

    def fill(obj) -> None:
        for f in dataclasses.fields(obj):
            if f.name not in obj.__dict__:
                if f.default is not dataclasses.MISSING:
                    setattr(obj, f.name, f.default)
                elif f.default_factory is not dataclasses.MISSING:
                    setattr(obj, f.name, f.default_factory())

    fill(draft)
    fill(draft.book)
    for ch in draft.book.chapters:
        fill(ch)
        for b in ch.blocks + ch.notes:
            fill(b)
    return draft
