"""Reading one book aloud: the script chapter by chapter, then the M4B, then the upload.

Each finished chapter is kept as a FLAC file in a folder for the job, so a job that is interrupted (the laptop
slept, the worker restarted) and claimed again continues from the first unfinished chapter. Progress goes to
the site every few seconds from a separate thread; it also tells the site the worker is still alive, and its
answer tells the worker when the owner has deleted the job.

Being alive is not the same as reading: a worker once hung for good loading a library, while that thread kept
telling the site all was well. So the same thread watches the reading too, and when nothing has been read for
STALL_SECONDS it ends the process with EXIT_STALLED. Whatever runs the worker starts it again (the local app,
or Docker on the server); the site queues the book again once the heartbeats stop, and it continues from its
finished chapters.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from .audio import RATE, ChapterAudio, Encoder, build_m4b, normalize, silence
from .client import Client, Gone
from .engines import Engine, Engines
from .text import PAUSE_BREAK, PAUSE_CHAPTER, PAUSE_HEADING, PAUSE_PARAGRAPH, chapter_name, intro

log = logging.getLogger(__name__)

PROGRESS_SECONDS = 10
# The longest a single paragraph may take: about 5 minutes for the longest paragraphs on the server's processor,
# and Chatterbox makes a sentence up to three times.
STALL_SECONDS = 20 * 60
EXIT_STALLED = 3  # the exit code that asks to be started again
SPEED_WINDOW = 5 * 60  # the speed the site shows is measured over the last few minutes of reading
KEEP_DAYS = 14  # unfinished work of jobs that never came back is removed after this


class Cancelled(Exception):
    pass


class _Progress(threading.Thread):
    def __init__(self, client: Client, job_id: str, total: int, stall_seconds: float = STALL_SECONDS):
        super().__init__(daemon=True)
        self.client, self.job_id, self.total = client, job_id, total
        self.stall_seconds = stall_seconds
        self.active = time.monotonic()  # when the last paragraph started or finished
        self.reading = True  # watched for stalls only while reading: building the M4B takes long too
        self.recent: deque[tuple[float, float]] = deque()  # (when a paragraph was finished, seconds of audio)
        self.done = 0
        self.lines: list[str] = []
        self.cancelled = threading.Event()
        self.stopped = threading.Event()
        self.lock = threading.Lock()

    def say(self, line: str) -> None:
        log.info("%s", line)
        with self.lock:
            self.lines.append(f"{time.strftime('%H:%M:%S')} {line}")

    def touch(self) -> None:
        self.active = time.monotonic()

    def made(self, seconds: float) -> None:
        self.recent.append((time.monotonic(), seconds))

    def speed(self) -> float | None:
        """Seconds of audio made per second over the last SPEED_WINDOW, the time between paragraphs included
        (saving chapters, preparing text); None until there is a minute of audio to go by."""
        if not self.reading:
            return None  # building the M4B: there is no reading speed to tell
        now = time.monotonic()
        while self.recent and self.recent[0][0] < now - SPEED_WINDOW:
            self.recent.popleft()
        if len(self.recent) < 2:
            return None
        audio = sum(seconds for _, seconds in list(self.recent)[1:])  # the first one marks the window's start
        elapsed = self.recent[-1][0] - self.recent[0][0]
        return round(min(audio / elapsed, 999), 2) if audio >= 60 and elapsed > 0 else None  # the site takes up to 1000

    def run(self) -> None:
        while not self.stopped.wait(PROGRESS_SECONDS):
            if self.reading and time.monotonic() - self.active > self.stall_seconds:
                self.stall()
                return
            self.send()

    def stall(self) -> None:
        minutes = (time.monotonic() - self.active) / 60
        log.error("Nothing read for %.0f minutes: the voice engine is stuck. Stopping, to be started again", minutes)
        self.say(f"Nothing was read for {minutes:.0f} minutes; starting the voice worker again")
        self.send()
        _exit(EXIT_STALLED)

    def send(self) -> None:
        with self.lock:
            lines, self.lines = self.lines, []
        try:
            self.client.progress(self.job_id, self.done, self.total, lines, self.speed())
        except Gone:
            self.cancelled.set()
        except Exception as exc:  # noqa: BLE001 -- the site is unreachable: keep reading, report later
            log.warning("Progress not sent: %s", exc)
            with self.lock:
                self.lines[:0] = lines


class Speeds:
    """How fast this computer makes each engine and quality ("blue/smooth": times faster than real time), measured
    on the chapters it reads and kept in a small file. The site uses it to say how long a book will take."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.totals: dict[str, list[float]] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.totals = {}

    def add(self, key: str, audio: float, work: float) -> None:
        a, w = self.totals.get(key, (0.0, 0.0))
        a, w = a + audio, w + work
        if a > 36000:  # past ten hours of audio, halve: recent chapters count most
            a, w = a / 2, w / 2
        self.totals[key] = [a, w]
        try:
            self.path.write_text(json.dumps(self.totals), encoding="utf-8")
        except OSError as exc:
            log.warning("Could not save the measured speeds: %s", exc)

    def ratios(self) -> dict[str, float]:
        return {key: round(a / w, 2) for key, (a, w) in self.totals.items() if w > 0 and a >= 60}


class Reader:
    def __init__(self, client: Client, engines: Engines, cache: Path, speeds: Speeds | None = None,
                 stall_seconds: float = STALL_SECONDS):
        self.client = client
        self.engines = engines
        self.cache = cache
        self.speeds = speeds
        self.stall_seconds = stall_seconds

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
        progress = _Progress(self.client, job_id, total, self.stall_seconds)
        engine = self.engines.get(o["engine"])
        progress.say(f"Reading {len(chapters)} chapters with {o['engine']} / {o['voice']}" + (f" (attempt {job['attempts']})" if job.get("attempts", 1) > 1 else ""))
        progress.touch()  # loading the engine (a download, the first time) is not reading
        progress.start()
        started = time.monotonic()
        encoder = Encoder(work / "book.aac")  # every chapter, as soon as it is there (those read before too)
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
                    began = time.monotonic()
                    audio = self._chapter(engine, o, lang, chapter, script if n == 0 else None, progress)
                    if self.speeds is not None:
                        self.speeds.add(f"{o['engine']}/{o['quality']}", len(audio) / RATE, time.monotonic() - began)
                    tmp = path.with_suffix(".tmp.flac")
                    sf.write(str(tmp), audio, RATE, subtype="PCM_16")
                    tmp.replace(path)
                    seconds = len(audio) / RATE
                    elapsed = time.monotonic() - started
                    progress.say(f"Chapter {n + 1} of {len(chapters)}: {title} ({seconds / 60:.0f} min of audio, {elapsed / 60:.0f} min so far)")
                done.append(ChapterAudio(title, path, seconds))
                encoder.add(path)
            progress.reading = False
            progress.say("Building the audiobook file")
            out = work / "book.m4b"
            length = sum(c.seconds for c in done)
            encoded = encoder.finish(timeout=600 + length / 4)
            if encoded is None:
                log.warning("The audio encoded while reading is incomplete (%s); encoding the book now", encoder.error)
            build_m4b(done, script["title"], script["authors"], lang, cover, out, encoded)
            progress.say(f"Uploading {out.stat().st_size / 1048576:.0f} MB ({length / 3600:.1f} hours of audio)")
            progress.stopped.set()
            progress.send()
            self.client.upload(job_id, out, length, len(done))
            log.info("Job %s done in %.0f min", job_id, (time.monotonic() - started) / 60)
            shutil.rmtree(work, ignore_errors=True)
        except (Cancelled, Gone):
            log.info("Job %s was deleted or taken over; stopping", job_id)
            encoder.stop()  # first: on Windows its open file would stay behind
            shutil.rmtree(work, ignore_errors=True)
        finally:
            encoder.stop()
            progress.stopped.set()

    def _chapter(self, engine: Engine, o: dict, lang: str, chapter: dict, opening: dict | None, progress: _Progress) -> np.ndarray:
        def speak(text: str, text_lang: str) -> np.ndarray:
            if progress.cancelled.is_set():
                raise Cancelled
            progress.touch()
            audio = engine.synthesize(text, text_lang, o["voice"], o["speed"], o["quality"])
            progress.touch()
            progress.made(len(audio) / RATE)
            return audio

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


def _exit(code: int) -> None:
    """End the whole process at once: the thread that is stuck cannot be stopped any other way. (No
    logging.shutdown(): it waits for the handlers' locks, which the stuck thread may hold. Each line is written
    to the log file as it is logged.)"""
    os._exit(code)


def _lang_of(text: str, default: str) -> str:
    hebrew = sum("א" <= ch <= "ת" for ch in text)
    latin = sum(ch.isascii() and ch.isalpha() for ch in text)
    return "he" if hebrew > latin else "en" if latin > hebrew else default
