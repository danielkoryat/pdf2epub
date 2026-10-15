"""Conversion jobs for the web UI: stored on disk, run one at a time on a background thread.

A job goes queued -> preparing -> review -> building -> done (or failed). With review switched off the
build starts as soon as preparing is finished. Each job lives in its own folder:
input.pdf, job.json (status), draft.pkl (the prepared book), book.epub.
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
from ..pipeline import Draft, Options, build, prepare
from ..validate import check_epub

log = logging.getLogger(__name__)

ACTIVE = ("queued", "preparing", "building")
STAGE_LABELS = {
    "analyse": "Analysing pages",
    "extract": "Reading pages",
    "structure": "Finding chapters",
    "write": "Writing EPUB",
}


@dataclass
class Job:
    id: str
    filename: str
    created: float
    options: dict
    review: bool = True
    status: str = "queued"
    stage: str = ""
    done: int = 0
    total: int = 0
    error: str = ""
    log: list[str] = field(default_factory=list)
    summary: dict | None = None  # facts about the prepared draft
    report: dict | None = None  # facts about the written EPUB
    edits: dict | None = None  # the last edits the EPUB was built with
    title: str = ""

    def public(self) -> dict:
        data = asdict(self)
        data["stage_label"] = STAGE_LABELS.get(self.stage, self.stage)
        data["log"] = self.log[-8:]
        return data


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
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self._queue: queue.Queue[tuple[str, str, dict | None]] = queue.Queue()
        self._lock = threading.Lock()
        self._drafts: OrderedDict[str, Draft] = OrderedDict()
        self._log_handler = _JobLogHandler()
        logging.getLogger().addHandler(self._log_handler)
        self._load()
        self._worker = threading.Thread(target=self._run, name="pdf2epub-worker", daemon=True)
        self._worker.start()

    # -- persistence ----------------------------------------------------------------------------------------

    def dir(self, job_id: str) -> Path:
        return self.root / job_id

    def _save(self, job: Job) -> None:
        path = self.dir(job.id) / "job.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(job), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    def _load(self) -> None:
        for path in sorted(self.root.glob("*/job.json")):
            try:
                job = Job(**json.loads(path.read_text(encoding="utf-8")))
            except (OSError, TypeError, ValueError):
                log.warning("Skipping unreadable job %s", path.parent.name)
                continue
            if job.status in ACTIVE:
                job.status, job.error = "failed", "Interrupted: the server stopped while this job was running."
                self._save(job)
            self.jobs[job.id] = job

    # -- public API ----------------------------------------------------------------------------------------

    def list(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def create(self, filename: str, options: dict, review: bool) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], filename=filename, created=time.time(), options=options, review=review)
        job.title = options.get("title") or Path(filename).stem.replace("_", " ")
        self.dir(job.id).mkdir(parents=True)
        return job

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
        shutil.rmtree(self.dir(job.id), ignore_errors=True)

    def draft(self, job: Job) -> Draft:
        """The prepared draft of a job (cached: the review page asks for it repeatedly)."""
        with self._lock:
            if job.id in self._drafts:
                self._drafts.move_to_end(job.id)
                return self._drafts[job.id]
        with open(self.dir(job.id) / "draft.pkl", "rb") as fh:
            draft = pickle.load(fh)
        with self._lock:
            self._drafts[job.id] = draft
            while len(self._drafts) > 2:
                self._drafts.popitem(last=False)
        return draft

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
                    self._prepare(job)
                else:
                    self._build(job, edits or {})
            except Exception as exc:  # noqa: BLE001 -- any failure is reported on the job
                log.error("Job %s failed: %s", job.id, exc)
                job.status, job.error = "failed", str(exc) or exc.__class__.__name__
                job.log.append(traceback.format_exc(limit=3))
                self._save(job)
            finally:
                self._log_handler.job = None

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
        )
        draft = prepare(self.dir(job.id) / "input.pdf", opts, self._progress(job))
        with open(self.dir(job.id) / "draft.pkl", "wb") as fh:
            pickle.dump(draft, fh, protocol=pickle.HIGHEST_PROTOCOL)
        with self._lock:
            self._drafts.pop(job.id, None)
        job.title = draft.book.title
        job.summary = {
            "pages": draft.pages,
            "scanned_pages": draft.scanned_pages,
            "ocr_lang": draft.ocr_lang,
            "ocr_fixes": draft.ocr_fixes,
            "seconds": draft.seconds,
            "warnings": draft.warnings,
            "chapters": len(draft.book.chapters),
            "toc_entries": sum(ch.in_toc for ch in draft.book.chapters),
        }
        if job.review:
            job.status = "review"
            self._save(job)
        else:
            self._build(job, {})

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
        job.title = book.title
        job.status = "done"
        self._save(job)
