"""The conversion worker: takes jobs from the database queue and runs them.

  * server mode: `pdf2epub-worker` runs in its own sandboxed container (no network, not root, read-only system,
    memory and process limits; see docker/). Each job runs in a separate child process with resource limits and
    a time limit, so a hostile file can at worst crash or stall its own conversion, which is then stopped,
    marked failed, and its pages refunded.
  * local mode: the web app runs the worker as a thread, jobs in-process, as before.

The worker writes a heartbeat while a job runs; jobs whose worker stopped are queued again (or failed after
repeated attempts). Progress and recent log lines go to the job's row, where the web app reads them.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import asdict

from ..draftio import save_draft
from ..edit import Edits, apply_edits
from ..enrich import EnrichOptions, EpubPackage, enrich_epub, inspect_epub
from ..narration import narration_script, save_script
from ..pipeline import Draft, Options, build, prepare
from ..validate import check_epub
from ..voices import estimate, measured_speeds
from .db import Database
from .jobs import Job, Library
from .quota import Quota
from .settings import Settings
from .voice import online_workers

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 15
STALE_SECONDS = 180


class JobFailed(Exception):
    """A failure whose message is meant for the user."""


class _Reporter:
    """Writes progress and the job's log lines to the database, at most about once a second."""

    def __init__(self, db: Database, job: Job):
        self.db = db
        self.job = job
        self._last = 0.0
        self._lock = threading.Lock()

    def progress(self, stage: str, done: int, total: int) -> None:
        changed = stage != self.job.stage
        self.job.stage, self.job.done, self.job.total = stage, done, total
        self.flush(force=changed)

    def log_line(self, line: str) -> None:
        with self._lock:
            self.job.log.append(line)
            del self.job.log[:-200]
        self.flush()

    def flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last < 1.0:
            return
        self._last = now
        with self._lock:
            lines = list(self.job.log)
        self.db.update_job(self.job.id, stage=self.job.stage, done=self.job.done, total=self.job.total, log=lines, heartbeat=time.time())


class _LogHandler(logging.Handler):
    def __init__(self, reporter: _Reporter):
        super().__init__(logging.INFO)
        self.reporter = reporter
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("pdf2epub") or record.levelno >= logging.WARNING:
            self.reporter.log_line(self.format(record))


# ---------------------------------------------------------------------------------------------------------
# Running one action of one job


def run_action(settings: Settings, db: Database, library: Library, job_id: str) -> None:
    """Run the pending action of a claimed job to its end (success or failure)."""
    row = db.job(job_id)
    if row is None:
        return
    job = Job.from_row(row)
    action = job.pending
    reporter = _Reporter(db, job)
    handler = _LogHandler(reporter)
    logging.getLogger().addHandler(handler)
    quota = Quota(db, settings.limits)
    try:
        if job.kind == "audio":
            _prepare_audio(settings, library, db, job, reporter)  # the voices are made elsewhere (voice.py)
        elif job.kind == "epub":
            _prepare_epub(library, db, job, reporter) if action == "prepare" else _build_epub(library, db, job, reporter, job.edits or {})
        else:
            _prepare_pdf(settings, library, db, quota, job, reporter) if action == "prepare" else _build_pdf(library, db, job, reporter, job.edits or {})
    except Exception as exc:  # noqa: BLE001 -- every failure ends the job with a message
        message = str(exc) if isinstance(exc, JobFailed) else "The conversion failed. Please check the file and try again."
        log.error("Job %s failed: %s", job.id, exc)
        if not isinstance(exc, JobFailed):
            reporter.log_line(traceback.format_exc(limit=3))
        if job.kind == "pdf" and action == "prepare" and settings.server:
            quota.refund(job.owner_id, job.id)
        reporter.flush(force=True)
        db.update_job(job.id, status="failed", pending=None, error=message[:500], size=library.folder_size(job.id))
    finally:
        logging.getLogger().removeHandler(handler)


def fail_job(db: Database, settings: Settings, job_id: str, message: str) -> None:
    """End a job from outside its process (time limit, crash) and refund what it was charged."""
    row = db.job(job_id)
    if row is None:
        return
    db.update_job(job_id, status="failed", pending=None, error=message)
    if row["kind"] == "pdf" and row["pending"] == "prepare" and settings.server:
        Quota(db, settings.limits).refund(row["owner_id"], job_id)


def _finish_prepare(library: Library, db: Database, job: Job, **fields) -> None:
    if job.review:
        db.update_job(job.id, status="review", pending=None, size=library.folder_size(job.id), **fields)
    else:
        db.update_job(job.id, status="queued", pending="build", edits={}, size=library.folder_size(job.id), **fields)


def _prepare_pdf(settings: Settings, library: Library, db: Database, quota: Quota, job: Job, reporter: _Reporter) -> None:
    import pymupdf

    path = library.input_path(job)
    try:
        with pymupdf.open(path) as pdf:
            if pdf.needs_pass:
                raise JobFailed("The PDF is password-protected")
            pages = pdf.page_count
    except JobFailed:
        raise
    except Exception:
        raise JobFailed("The file could not be opened as a PDF") from None
    limits = settings.limits
    if settings.server:
        role = (db.user(job.owner_id) or {}).get("role")
        if pages > limits.max_pdf_pages and role != "admin":
            raise JobFailed(f"The PDF has {pages} pages; the limit is {limits.max_pdf_pages}")
        if not quota.charge(job.owner_id, job.id, pages, job.ip):
            left = quota.balance(job.owner_id, job.ip).available
            more = ("Sign in with Google to get more pages." if role == "guest"
                    else "More pages will be available to buy soon.")
            raise JobFailed(f"This book has {pages} pages but you have {left} pages left. {more}")
    db.update_job(job.id, pages=pages)
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
    draft = prepare(path, opts, reporter.progress)
    save_draft(draft, library.draft_path(job))
    book = draft.book
    summary = {
        "pages": draft.pages,
        "scanned_pages": draft.scanned_pages,
        "ocr_lang": draft.ocr_lang,
        "ocr_fixes": draft.ocr_fixes,
        "seconds": draft.seconds,
        "warnings": draft.warnings,
        "chapters": len(book.chapters),
        "toc_entries": sum(ch.in_toc for ch in book.chapters),
    }
    reporter.flush(force=True)
    _finish_prepare(library, db, job, title=book.title, authors=", ".join(book.authors), language=book.language, summary=summary)


def _build_pdf(library: Library, db: Database, job: Job, reporter: _Reporter, edits: dict) -> None:
    draft = library.draft(job)
    book = apply_edits(draft.book, Edits.from_json(edits))
    edited = Draft(**{**draft.__dict__, "book": book})
    out = library.epub_path(job)
    report = build(edited, out, reporter.progress)
    check = check_epub(out)
    data = asdict(report)
    data["size"] = out.stat().st_size
    data["validation"] = None if check is None else {"ok": check.ok, "errors": check.errors[:20], "warnings": len(check.warnings)}
    reporter.flush(force=True)
    db.update_job(job.id, status="done", pending=None, report=data, title=book.title, authors=", ".join(book.authors),
                  language=book.language, size=library.folder_size(job.id))


def _prepare_epub(library: Library, db: Database, job: Job, reporter: _Reporter) -> None:
    started = time.monotonic()
    reporter.progress("inspect", 0, 1)
    path = library.input_path(job)
    data = path.read_bytes()
    try:
        info = inspect_epub(data)
    except Exception:
        raise JobFailed("The EPUB could not be read: it may be damaged") from None
    pkg = EpubPackage(data)
    cover = pkg.cover_item()
    cover_bytes = pkg.files.get(pkg.item_path(cover)) if cover is not None else None
    # Only real JPEG/PNG covers are kept: the web app serves this file back to the browser.
    if cover_bytes and (cover_bytes[:3] == b"\xff\xd8\xff" or cover_bytes[:8] == b"\x89PNG\r\n\x1a\n"):
        library.cover_original_path(job).write_bytes(cover_bytes)
    before = check_epub(path)
    summary = {
        **asdict(info),
        "validation_before": None if before is None else {"ok": before.ok, "errors": before.errors[:20], "count": len(before.errors)},
        "seconds": round(time.monotonic() - started, 1),
    }
    log.info("EPUB %s: %d suggestions", info.version, len(info.suggestions))
    reporter.flush(force=True)
    _finish_prepare(library, db, job, title=job.options.get("title") or info.title or job.title,
                    authors=", ".join(info.authors), language=info.language or info.detected_language or "", summary=summary)


def _build_epub(library: Library, db: Database, job: Job, reporter: _Reporter, edits: dict) -> None:
    started = time.monotonic()
    reporter.progress("enrich", 0, 1)
    o, e = job.options, edits
    authors = e.get("authors", o.get("author"))
    if isinstance(authors, str):
        authors = [a.strip() for a in authors.split(",") if a.strip()]
    upload = library.cover_upload_path(job)
    opts = EnrichOptions(
        title=(e.get("title") or o.get("title") or "").strip() or None,
        authors=authors if authors else None,
        language=(e.get("language") or o.get("language") or "").strip() or None,
        cover_image=upload.read_bytes() if upload.is_file() and e.get("new_cover", True) else None,
        **{k: bool(e[k]) for k in ("fix_language", "direction", "contents_page", "landmarks", "accessibility", "cover", "upgrade", "ocr_fixes") if k in e},
    )
    data, changes = enrich_epub(library.input_path(job).read_bytes(), opts)
    out = library.epub_path(job)
    out.write_bytes(data)
    for change in changes:
        log.info("%s", change)
    check = check_epub(out)
    info = inspect_epub(data)
    report = {
        "changes": changes,
        "size": out.stat().st_size,
        "toc_entries": info.toc_entries,
        "remaining": info.suggestions,
        "seconds": round(time.monotonic() - started, 1),
        "validation": None if check is None else {"ok": check.ok, "errors": check.errors[:20], "warnings": len(check.warnings)},
    }
    reporter.flush(force=True)
    db.update_job(job.id, status="done", pending=None, report=report, title=info.title or job.title,
                  authors=", ".join(info.authors), language=info.language or "", size=library.folder_size(job.id))


def _prepare_audio(settings: Settings, library: Library, db: Database, job: Job, reporter: _Reporter) -> None:
    """The text an audiobook reads, from a copy of the finished book; then the job waits for a voice worker."""
    reporter.progress("narrate", 0, 1)
    data = library.input_path(job).read_bytes()
    authors = [a.strip() for a in job.authors.split(",") if a.strip()]
    try:
        script = narration_script(data, title=job.title, authors=authors, language=job.options["language"])
    except Exception:
        raise JobFailed("The book's text could not be read") from None
    if not script.chapters:
        raise JobFailed("The book has no text to read aloud")
    limit = settings.limits.max_audiobook_chars
    if settings.server and script.characters > limit and (db.user(job.owner_id) or {}).get("role") != "admin":
        raise JobFailed(f"The book is too long for an audiobook here ({script.characters:,} letters; the limit is {limit:,})")
    save_script(script, library.script_path(job))
    pkg = EpubPackage(data)
    cover = pkg.cover_item()
    cover_bytes = pkg.files.get(pkg.item_path(cover)) if cover is not None else None
    if cover_bytes and (cover_bytes[:3] == b"\xff\xd8\xff" or cover_bytes[:8] == b"\x89PNG\r\n\x1a\n"):
        library.audio_cover_path(job).write_bytes(cover_bytes)
    speeds = measured_speeds(online_workers(db) or db.voice_workers())
    summary = {"chapters": len(script.chapters), "characters": script.characters, **estimate(script.characters, job.options, speeds)}
    log.info("Audiobook text: %d chapters, %d letters", len(script.chapters), script.characters)
    reporter.flush(force=True)
    # attempts start again: they count the voice worker's tries from here on
    db.update_job(job.id, status="queued", pending="speak", stage="", done=0, total=0, attempts=0, summary=summary,
                  size=library.folder_size(job.id))


# ---------------------------------------------------------------------------------------------------------
# The worker loop


class Worker:
    def __init__(self, settings: Settings, db: Database, library: Library, isolate: bool, name: str = ""):
        self.settings = settings
        self.db = db
        self.library = library
        self.isolate = isolate  # run each job in a child process (server mode)
        self.name = name or f"{socket.gethostname()}-{os.getpid()}"
        self.stop = threading.Event()

    def run(self) -> None:
        last_sweep = 0.0
        log.info("Worker %s started (%s)", self.name, "isolated jobs" if self.isolate else "in-process jobs")
        while not self.stop.is_set():
            if time.monotonic() - last_sweep > 30:
                for job_id in self.db.requeue_stale(STALE_SECONDS):
                    log.warning("Job %s lost its worker; queued again or failed", job_id)
                last_sweep = time.monotonic()
            row = self.db.claim_job(self.name)
            if row is None:
                self.stop.wait(1.0)
                continue
            log.info("Job %s: %s (%s)", row["id"], row["pending"], row["kind"])
            if self.isolate:
                self._run_isolated(row["id"])
            else:
                self._run_in_process(row["id"])

    def _heartbeat(self, job_id: str, done: threading.Event) -> None:
        while not done.wait(HEARTBEAT_SECONDS):
            self.db.update_job(job_id, heartbeat=time.time())

    def _run_in_process(self, job_id: str) -> None:
        done = threading.Event()
        beat = threading.Thread(target=self._heartbeat, args=(job_id, done), daemon=True)
        beat.start()
        try:
            run_action(self.settings, self.db, self.library, job_id)
        finally:
            done.set()

    def _run_isolated(self, job_id: str) -> None:
        cmd = [sys.executable, "-m", "pdf2epub.web.worker", "--run-job", job_id]
        posix = os.name == "posix"
        child = subprocess.Popen(cmd, env=child_environment(self.settings), preexec_fn=_limit_child if posix else None, start_new_session=posix)
        done = threading.Event()
        beat = threading.Thread(target=self._heartbeat, args=(job_id, done), daemon=True)
        beat.start()
        try:
            child.wait(timeout=self.settings.limits.job_timeout_minutes * 60)
        except subprocess.TimeoutExpired:
            _kill_group(child)
            fail_job(self.db, self.settings, job_id, f"The conversion took longer than {self.settings.limits.job_timeout_minutes} minutes and was stopped")
            log.warning("Job %s stopped: time limit", job_id)
        finally:
            done.set()
        row = self.db.job(job_id)
        if row and row["status"] in ("preparing", "building"):  # the child died without finishing the job
            fail_job(self.db, self.settings, job_id, "The conversion stopped unexpectedly. Please try again.")
            log.warning("Job %s: worker process exited with %s", job_id, child.returncode)


_SECRET_WORDS = ("SECRET", "TOKEN", "PASSWORD", "PASSWD", "API_KEY", "PRIVATE", "CREDENTIAL", "GOOGLE_CLIENT", "POSTHOG", "AWS_", "SENTRY")


def child_environment(settings: Settings) -> dict[str, str]:
    """The environment of a job's process: this worker's settings, and no secrets of any kind."""
    env = {k: v for k, v in os.environ.items() if not any(word in k.upper() for word in _SECRET_WORDS)}
    env["PDF2EPUB_MODE"] = settings.mode
    env["PDF2EPUB_DATA"] = str(settings.data_dir)
    for name in settings.limits.__dataclass_fields__:
        env[f"PDF2EPUB_{name.upper()}"] = str(getattr(settings.limits, name))
    return env


def _limit_child() -> None:
    """Resource limits of a job's process (POSIX). Memory is capped by the worker container."""
    import resource

    gib = 1024**3
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * gib, 2 * gib))  # no single file larger than 2 GB
    resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.nice(5)


def _kill_group(child: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            import signal

            os.killpg(child.pid, signal.SIGKILL)  # the job and the Tesseract processes it started
        else:
            child.kill()
    except ProcessLookupError:
        pass
    child.wait()


def _setup_logging() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("pdf2epub").setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pdf2epub-worker", description="Run pdf2epub conversion jobs from the queue.")
    p.add_argument("--run-job", metavar="JOB_ID", help=argparse.SUPPRESS)  # internal: one job in this process
    p.add_argument("--in-process", action="store_true", help="run jobs in this process instead of child processes")
    args = p.parse_args(argv)
    _setup_logging()
    settings = Settings.from_env(web=False)
    # Temporary files (page images for OCR, unpacked EPUBs) go to the data disk, not to a small in-memory /tmp.
    # Each job's process and the Tesseract processes it starts inherit TMPDIR.
    tmp = settings.data_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(tmp)
    tempfile.tempdir = str(tmp)
    db = Database(settings.data_dir / "pdf2epub.db")
    library = Library(settings.data_dir, db, allow_pickle=not settings.server)
    if args.run_job:
        run_action(settings, db, library, args.run_job)
        return 0
    worker = Worker(settings, db, library, isolate=not args.in_process)
    if os.name == "posix":
        import signal

        # `docker stop` sends SIGTERM: finish the loop like Ctrl+C. A job that is running is picked up again
        # once its heartbeat goes stale.
        signal.signal(signal.SIGTERM, lambda *_: worker.stop.set())
    try:
        worker.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
