"""Jobs for the web UI, recorded in a SQLite database and run one at a time on a background thread.

Two kinds of job:
  * pdf:  convert a PDF. queued -> preparing (OCR, structure) -> review -> building -> done
  * epub: enrich an existing EPUB. queued -> preparing (inspection) -> review -> building -> done
With review switched off the build starts as soon as preparing is finished; a failure ends in "failed".

The job's record lives in data/pdf2epub.db (see db.py). Its files live in data/jobs/<id>/:
input.pdf or input.epub, draft.pkl (a prepared PDF conversion), cover-upload (a new cover for an EPUB),
book.epub (the result).
"""

from __future__ import annotations

import json
import logging
import pickle
import queue
import shutil
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..edit import Edits, apply_edits
from ..enrich import EnrichOptions, EpubPackage, enrich_epub, inspect_epub
from ..pipeline import Draft, Options, build, prepare
from ..validate import check_epub
from .db import JobDB

log = logging.getLogger(__name__)

ACTIVE = ("queued", "preparing", "building")
STAGE_LABELS = {
    "analyse": "Analysing pages",
    "extract": "Reading pages",
    "structure": "Finding chapters",
    "write": "Writing EPUB",
    "inspect": "Inspecting the EPUB",
    "enrich": "Enriching the EPUB",
}
# Fields kept only in memory: live progress of the running job.
_TRANSIENT = ("stage", "done", "total")


@dataclass
class Job:
    id: str
    filename: str
    created: float
    options: dict
    kind: str = "pdf"  # pdf | epub
    review: bool = True
    status: str = "queued"
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

    def public(self) -> dict:
        data = asdict(self)
        data["stage_label"] = STAGE_LABELS.get(self.stage, self.stage)
        data["log"] = self.log[-8:]
        return data

    def row(self) -> dict:
        data = asdict(self)
        for key in _TRANSIENT:
            data.pop(key)
        data["log"] = self.log[-200:]
        return data


def _fill_new_fields(draft: Draft) -> None:
    """Drafts saved by an older version lack fields added since; unpickling doesn't run __init__, so give
    them their defaults."""
    import dataclasses

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


class _JobLogHandler(logging.Handler):
    """Copies log records of the running job into job.log, so the UI can show what is happening."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.job: Job | None = None
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        job = self.job
        if job is None or (not record.name.startswith("pdf2epub") and record.levelno < logging.WARNING):
            return
        job.log.append(self.format(record))
        del job.log[:-200]


class JobStore:
    def __init__(self, data_dir: Path):
        self.root = data_dir / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = JobDB(data_dir / "pdf2epub.db")
        self.jobs: dict[str, Job] = {}
        self._queue: queue.Queue[tuple[str, str, dict | None]] = queue.Queue()
        self._lock = threading.Lock()
        self._drafts: OrderedDict[str, Draft] = OrderedDict()
        self._log_handler = _JobLogHandler()
        logging.getLogger().addHandler(self._log_handler)
        self._import_json_jobs()
        self._load()
        self._worker = threading.Thread(target=self._run, name="pdf2epub-worker", daemon=True)
        self._worker.start()

    # -- persistence ----------------------------------------------------------------------------------------

    def dir(self, job_id: str) -> Path:
        return self.root / job_id

    def _save(self, job: Job) -> None:
        self.db.save(job.row())

    def _import_json_jobs(self) -> None:
        """Import jobs recorded by earlier versions as data/jobs/<id>/job.json files."""
        known = self.db.ids()
        for path in sorted(self.root.glob("*/job.json")):
            if path.parent.name in known:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job = Job(**{k: v for k, v in data.items() if k in Job.__dataclass_fields__})
            except (OSError, TypeError, ValueError):
                log.warning("Skipping unreadable job %s", path.parent.name)
                continue
            self._save(job)
            log.info("Imported job %s into the database", job.id)

    def _load(self) -> None:
        for row in self.db.all():
            job = Job(**{k: v for k, v in row.items() if k in Job.__dataclass_fields__})
            if job.status in ACTIVE:
                job.status, job.error = "failed", "Interrupted: the server stopped while this job was running."
                self._save(job)
            self.jobs[job.id] = job

    # -- public API ----------------------------------------------------------------------------------------

    def list(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def create(self, filename: str, options: dict, review: bool, kind: str = "pdf") -> Job:
        job = Job(id=uuid.uuid4().hex[:12], filename=filename, created=time.time(), options=options, review=review, kind=kind)
        job.title = options.get("title") or Path(filename).stem.replace("_", " ")
        self.dir(job.id).mkdir(parents=True)
        return job

    def input_path(self, job: Job) -> Path:
        return self.dir(job.id) / f"input.{job.kind}"

    def cover_upload_path(self, job: Job) -> Path:
        return self.dir(job.id) / "cover-upload"

    def submit(self, job: Job) -> None:
        with self._lock:
            self.jobs[job.id] = job
            self._save(job)
        self._queue.put(("prepare", job.id, None))

    def request_build(self, job: Job, edits: dict) -> None:
        job.status, job.error, job.stage, job.done, job.total = "queued", "", "", 0, 0
        job.edits = edits
        self._save(job)
        self._queue.put(("build", job.id, edits))

    def discard(self, job: Job) -> None:
        """Remove a job that was created but never submitted (e.g. a rejected upload)."""
        shutil.rmtree(self.dir(job.id), ignore_errors=True)

    def delete(self, job: Job) -> None:
        if job.status in ACTIVE:
            raise RuntimeError("A running job cannot be deleted.")
        with self._lock:
            self.jobs.pop(job.id, None)
            self._drafts.pop(job.id, None)
        self.db.delete(job.id)
        shutil.rmtree(self.dir(job.id), ignore_errors=True)

    def draft(self, job: Job) -> Draft:
        """The prepared draft of a PDF job (cached: the review page asks for it repeatedly)."""
        with self._lock:
            if job.id in self._drafts:
                self._drafts.move_to_end(job.id)
                return self._drafts[job.id]
        with open(self.dir(job.id) / "draft.pkl", "rb") as fh:
            draft = pickle.load(fh)
        _fill_new_fields(draft)
        with self._lock:
            self._drafts[job.id] = draft
            while len(self._drafts) > 2:
                self._drafts.popitem(last=False)
        return draft

    def epub_cover(self, job: Job) -> bytes | None:
        """The cover of an EPUB job: a newly uploaded one, else the book's own."""
        upload = self.cover_upload_path(job)
        if upload.is_file():
            return upload.read_bytes()
        pkg = EpubPackage.open(self.input_path(job))
        item = pkg.cover_item()
        return pkg.files.get(pkg.item_path(item)) if item is not None else None

    def epub_path(self, job: Job) -> Path:
        return self.dir(job.id) / "book.epub"

    # -- worker --------------------------------------------------------------------------------------------

    def _progress(self, job: Job):
        def update(stage: str, done: int, total: int) -> None:
            job.stage, job.done, job.total = stage, done, total

        return update

    def _run(self) -> None:
        while True:
            action, job_id, edits = self._queue.get()
            job = self.jobs.get(job_id)
            if job is None:
                continue
            self._log_handler.job = job
            try:
                if action == "prepare":
                    self._prepare_epub(job) if job.kind == "epub" else self._prepare(job)
                else:
                    self._build_epub(job, edits or {}) if job.kind == "epub" else self._build(job, edits or {})
            except Exception as exc:  # noqa: BLE001 -- any failure is reported on the job
                log.error("Job %s failed: %s", job.id, exc)
                job.status, job.error = "failed", str(exc) or exc.__class__.__name__
                job.log.append(traceback.format_exc(limit=3))
                self._save(job)
            finally:
                self._log_handler.job = None

    def _finish_prepare(self, job: Job) -> None:
        if job.review:
            job.status = "review"
            self._save(job)
        elif job.kind == "epub":
            self._build_epub(job, {})
        else:
            self._build(job, {})

    def _prepare(self, job: Job) -> None:
        job.status = "preparing"
        self._save(job)
        o = job.options
        opts = Options(
            title=o.get("title") or None,
            authors=[a.strip() for a in (o.get("author") or "").split(",") if a.strip()],
            language=o.get("language") or None,
            ocr=o.get("ocr") or "auto",
            ocr_lang=o.get("ocr_lang") or None,
            cover=bool(o.get("cover", True)),
            source_name=job.filename,
        )
        draft = prepare(self.input_path(job), opts, self._progress(job))
        with open(self.dir(job.id) / "draft.pkl", "wb") as fh:
            pickle.dump(draft, fh, protocol=pickle.HIGHEST_PROTOCOL)
        with self._lock:
            self._drafts.pop(job.id, None)
        book = draft.book
        job.title, job.authors, job.language = book.title, ", ".join(book.authors), book.language
        job.summary = {
            "pages": draft.pages,
            "scanned_pages": draft.scanned_pages,
            "ocr_lang": draft.ocr_lang,
            "ocr_fixes": draft.ocr_fixes,
            "seconds": draft.seconds,
            "warnings": draft.warnings,
            "chapters": len(book.chapters),
            "toc_entries": sum(ch.in_toc for ch in book.chapters),
        }
        self._finish_prepare(job)

    def _build(self, job: Job, edits: dict) -> None:
        job.status = "building"
        self._save(job)
        draft = self.draft(job)
        book = apply_edits(draft.book, Edits.from_json(edits))
        edited = Draft(**{**draft.__dict__, "book": book})
        out = self.epub_path(job)
        report = build(edited, out, self._progress(job))
        check = check_epub(out)
        job.report = asdict(report)
        job.report["size"] = out.stat().st_size
        job.report["validation"] = (
            None if check is None else {"ok": check.ok, "errors": check.errors[:20], "warnings": len(check.warnings)}
        )
        job.title, job.authors, job.language = book.title, ", ".join(book.authors), book.language
        job.status = "done"
        self._save(job)

    def _prepare_epub(self, job: Job) -> None:
        job.status = "preparing"
        job.stage = "inspect"
        self._save(job)
        started = time.monotonic()
        info = inspect_epub(self.input_path(job).read_bytes())
        before = check_epub(self.input_path(job))
        job.title = job.options.get("title") or info.title or job.title
        job.authors, job.language = ", ".join(info.authors), info.language or info.detected_language or ""
        job.summary = {
            **asdict(info),
            "validation_before": None if before is None else {"ok": before.ok, "errors": before.errors[:20], "count": len(before.errors)},
            "seconds": round(time.monotonic() - started, 1),
        }
        log.info("EPUB %s: %d suggestions", info.version, len(info.suggestions))
        self._finish_prepare(job)

    def _build_epub(self, job: Job, edits: dict) -> None:
        job.status = "building"
        job.stage = "enrich"
        self._save(job)
        started = time.monotonic()
        o, e = job.options, edits
        authors = e.get("authors", o.get("author"))
        if isinstance(authors, str):
            authors = [a.strip() for a in authors.split(",") if a.strip()]
        upload = self.cover_upload_path(job)
        opts = EnrichOptions(
            title=(e.get("title") or o.get("title") or "").strip() or None,
            authors=authors if authors else None,
            language=(e.get("language") or o.get("language") or "").strip() or None,
            cover_image=upload.read_bytes() if upload.is_file() and e.get("new_cover", True) else None,
            **{k: bool(e[k]) for k in ("fix_language", "direction", "contents_page", "landmarks", "accessibility", "cover", "upgrade", "ocr_fixes") if k in e},
        )
        data, changes = enrich_epub(self.input_path(job).read_bytes(), opts)
        out = self.epub_path(job)
        out.write_bytes(data)
        for change in changes:
            log.info("%s", change)
        check = check_epub(out)
        info = inspect_epub(data)
        job.title, job.authors, job.language = info.title or job.title, ", ".join(info.authors), info.language or ""
        job.report = {
            "changes": changes,
            "size": out.stat().st_size,
            "toc_entries": info.toc_entries,
            "remaining": info.suggestions,
            "seconds": round(time.monotonic() - started, 1),
            "validation": None if check is None else {"ok": check.ok, "errors": check.errors[:20], "warnings": len(check.warnings)},
        }
        job.status = "done"
        self._save(job)
