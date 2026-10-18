"""Reading one book aloud: the script chapter by chapter, then the M4B, then the upload.

Each finished chapter is kept as a FLAC file in a folder for the job, so a job that is interrupted (the laptop
slept, the worker restarted) and claimed again continues from the first unfinished chapter. Progress goes to
the site every few seconds from a separate thread; it also tells the site the worker is still alive, and its
answer tells the worker when the owner has deleted the job.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from .audio import RATE, ChapterAudio, build_m4b, normalize, silence
from .client import Client, Gone
from .engines import Engine, Engines
from .text import PAUSE_BREAK, PAUSE_CHAPTER, PAUSE_HEADING, PAUSE_PARAGRAPH, chapter_name, intro

log = logging.getLogger(__name__)

PROGRESS_SECONDS = 10
KEEP_DAYS = 14  # unfinished work of jobs that never came back is removed after this


class Cancelled(Exception):
    pass


class _Progress(threading.Thread):
    def __init__(self, client: Client, job_id: str, total: int):
        super().__init__(daemon=True)
        self.client, self.job_id, self.total = client, job_id, total
        self.done = 0
        self.lines: list[str] = []
        self.cancelled = threading.Event()
        self.stopped = threading.Event()
        self.lock = threading.Lock()

    def say(self, line: str) -> None:
        log.info("%s", line)
        with self.lock:
            self.lines.append(f"{time.strftime('%H:%M:%S')} {line}")

    def run(self) -> None:
        while not self.stopped.wait(PROGRESS_SECONDS):
            self.send()

    def send(self) -> None:
        with self.lock:
            lines, self.lines = self.lines, []
        try:
            self.client.progress(self.job_id, self.done, self.total, lines)
        except Gone:
            self.cancelled.set()
        except Exception as exc:  # noqa: BLE001 -- the site is unreachable: keep reading, report later
            log.warning("Progress not sent: %s", exc)
            with self.lock:
                self.lines[:0] = lines


class Reader:
    def __init__(self, client: Client, engines: Engines, cache: Path):
        self.client = client
        self.engines = engines
        self.cache = cache

    def read(self, job: dict[str, Any]) -> None:
        job_id, o = job["id"], job["options"]
        script = self.client.script(job_id)
        cover = self.client.cover(job_id) if job.get("has_cover") else None
        key = hashlib.sha256(json.dumps([o, script], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        work = self.cache / f"{job_id}-{key}"
        work.mkdir(parents=True, exist_ok=True)
        self._forget_old(keep=work)

        chapters = script["chapters"]
        lang = script["language"]
        total = sum(len(ch["title"]) + sum(len(p["text"]) for p in ch["paragraphs"]) for ch in chapters)
        progress = _Progress(self.client, job_id, total)
        engine = self.engines.get(o["engine"])
        progress.say(f"Reading {len(chapters)} chapters with {o['engine']} / {o['voice']}" + (f" (attempt {job['attempts']})" if job.get("attempts", 1) > 1 else ""))
        progress.start()
        started = time.monotonic()
        try:
            done: list[ChapterAudio] = []
            for n, chapter in enumerate(chapters):
                title = chapter_name(chapter["title"], n + 1, lang)
                path = work / f"{n:04d}.flac"
                size = len(chapter["title"]) + sum(len(p["text"]) for p in chapter["paragraphs"])
                if path.is_file():  # read before this job was interrupted
                    seconds = sf.info(str(path)).duration
                    progress.done += size
                else:
                    progress.done = sum(len(c["title"]) + sum(len(p["text"]) for p in c["paragraphs"]) for c in chapters[:n])
                    audio = self._chapter(engine, o, lang, chapter, script if n == 0 else None, progress)
                    tmp = path.with_suffix(".tmp.flac")
                    sf.write(str(tmp), audio, RATE, subtype="PCM_16")
                    tmp.replace(path)
                    seconds = len(audio) / RATE
                    elapsed = time.monotonic() - started
                    progress.say(f"Chapter {n + 1} of {len(chapters)}: {title} ({seconds / 60:.0f} min of audio, {elapsed / 60:.0f} min so far)")
                done.append(ChapterAudio(title, path, seconds))
            progress.say("Building the audiobook file")
            out = work / "book.m4b"
            build_m4b(done, script["title"], script["authors"], lang, cover, out)
            length = sum(c.seconds for c in done)
            progress.say(f"Uploading {out.stat().st_size / 1048576:.0f} MB ({length / 3600:.1f} hours of audio)")
            progress.stopped.set()
            progress.send()
            self.client.upload(job_id, out, length, len(done))
            log.info("Job %s done in %.0f min", job_id, (time.monotonic() - started) / 60)
            shutil.rmtree(work, ignore_errors=True)
        except (Cancelled, Gone):
            log.info("Job %s was deleted or taken over; stopping", job_id)
            shutil.rmtree(work, ignore_errors=True)
        finally:
            progress.stopped.set()

    def _chapter(self, engine: Engine, o: dict, lang: str, chapter: dict, opening: dict | None, progress: _Progress) -> np.ndarray:
        def speak(text: str, text_lang: str) -> np.ndarray:
            if progress.cancelled.is_set():
                raise Cancelled
            return engine.synthesize(text, text_lang, o["voice"], o["speed"], o["quality"])

        parts: list[np.ndarray] = []
        if opening is not None:  # the book opens with its title and author
            parts += [speak(intro(opening["title"], opening["authors"], lang), lang), silence(PAUSE_HEADING * 1.5)]
        if chapter["title"]:
            parts += [speak(chapter["title"], _lang_of(chapter["title"], lang)), silence(PAUSE_HEADING)]
            progress.done += len(chapter["title"])
        group: list[dict] = []

        def flush() -> None:
            if group:
                text = "\n".join(p["text"] for p in group)
                parts.extend([speak(text, group[0]["lang"]), silence(PAUSE_PARAGRAPH)])
                progress.done += sum(len(p["text"]) for p in group)
                group.clear()

        for p in chapter["paragraphs"]:
            if p["kind"] == "break":
                flush()
                parts.append(silence(PAUSE_BREAK))
            elif p["kind"] == "heading" or not engine.group_chars:
                flush()
                parts += [speak(p["text"], p["lang"]), silence(PAUSE_HEADING if p["kind"] == "heading" else PAUSE_PARAGRAPH)]
                progress.done += len(p["text"])
            else:
                if group and (group[0]["lang"] != p["lang"] or sum(len(g["text"]) for g in group) + len(p["text"]) > engine.group_chars):
                    flush()
                group.append(p)
        flush()
        audio = normalize(np.concatenate(parts)) if parts else silence(0)
        return np.concatenate([audio, silence(PAUSE_CHAPTER)])

    def _forget_old(self, keep: Path) -> None:
        cutoff = time.time() - KEEP_DAYS * 86400
        for folder in self.cache.iterdir():
            if folder != keep and folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)


def _lang_of(text: str, default: str) -> str:
    hebrew = sum("א" <= ch <= "ת" for ch in text)
    latin = sum(ch.isascii() and ch.isalpha() for ch in text)
    return "he" if hebrew > latin else "en" if latin > hebrew else default
