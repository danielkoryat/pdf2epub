"""Sound plumbing: silences, resampling, loudness, and building the M4B (chapters, cover, tags) with ffmpeg."""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

RATE = 24_000  # the audiobook's sample rate: plenty for speech, and what Chatterbox and Edge produce
BITRATE = "48k"  # mono AAC: about 21 MB per hour
# The worker may run without a console (started at logon): don't flash a window for every ffmpeg call.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
# Encoding while the book is read gives way to the voices (on the server they share 1.5 processor cores).
LOW_PRIORITY: dict = ({"creationflags": NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS} if os.name == "nt"
                      else {"preexec_fn": lambda: os.nice(10)})


def ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg was not found: install it and make sure it is on PATH")
    return path


def silence(seconds: float, rate: int = RATE) -> np.ndarray:
    return np.zeros(int(seconds * rate), dtype=np.float32)


def resample(audio: np.ndarray, rate: int, target: int = RATE) -> np.ndarray:
    if rate == target:
        return audio.astype(np.float32, copy=False)
    # soxr itself, not through librosa: importing librosa loads SciPy's OpenBLAS, and on Windows that load once
    # hung for good inside a worker whose ONNX Runtime threads were already running.
    import soxr

    return soxr.resample(audio.astype(np.float32), rate, target, quality="HQ").astype(np.float32)


def _pipe(args: list[str], data: bytes) -> bytes:
    done = subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", *args], input=data, capture_output=True, check=False,
                          creationflags=NO_WINDOW)
    if done.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {done.stderr.decode(errors='replace')[-300:]}")
    return done.stdout


def decode(data: bytes, rate: int = RATE) -> np.ndarray:
    """Any audio file (e.g. MP3) as mono float samples."""
    raw = _pipe(["-i", "pipe:0", "-f", "f32le", "-ac", "1", "-ar", str(rate), "pipe:1"], data)
    return np.frombuffer(raw, dtype=np.float32).copy()


def change_tempo(audio: np.ndarray, speed: float, rate: int = RATE) -> np.ndarray:
    """Faster or slower speech at the same pitch (ffmpeg's atempo)."""
    if abs(speed - 1.0) < 1e-3 or not len(audio):
        return audio
    fmt = ["-f", "f32le", "-ac", "1", "-ar", str(rate)]
    raw = _pipe([*fmt, "-i", "pipe:0", "-filter:a", f"atempo={speed}", *fmt, "pipe:1"], audio.astype(np.float32).tobytes())
    return np.frombuffer(raw, dtype=np.float32).copy()


def normalize(audio: np.ndarray, target_db: float = -20.0, peak: float = 0.95) -> np.ndarray:
    """Even loudness from chapter to chapter: scale the speech (ignoring silences) to target_db RMS, without
    letting peaks clip."""
    if not len(audio):
        return audio
    voiced = audio[np.abs(audio) > 0.01]
    if not len(voiced):
        return audio
    rms = float(np.sqrt(np.mean(voiced.astype(np.float64) ** 2)))
    gain = 10 ** (target_db / 20) / max(rms, 1e-6)
    top = float(np.max(np.abs(audio))) * gain
    if top > peak:
        gain *= peak / top
    return (audio * gain).astype(np.float32)


@dataclass
class ChapterAudio:
    title: str
    path: Path  # FLAC
    seconds: float


def _escape(value: str) -> str:
    """ffmetadata escaping: =, ;, #, backslash and newlines need a backslash."""
    out = []
    for ch in value:
        if ch in "=;#\\\n":
            out.append("\\")
        out.append(ch)
    return "".join(out)


def ffmetadata(title: str, authors: list[str], chapters: list[ChapterAudio], language: str) -> str:
    """Tags and chapter marks in ffmpeg's metadata format (times in milliseconds)."""
    who = ", ".join(authors)
    lines = [";FFMETADATA1", f"title={_escape(title)}", f"album={_escape(title)}", f"artist={_escape(who)}",
             f"album_artist={_escape(who)}", "genre=Audiobook", f"language={_escape(language)}"]
    start = 0
    for ch in chapters:
        end = start + round(ch.seconds * 1000)
        lines += ["", "[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}", f"title={_escape(ch.title)}"]
        start = end
    return "\n".join(lines) + "\n"


class Encoder:
    """The book's AAC, made while it is read: one ffmpeg for the whole book, fed each chapter (its FLAC) as soon
    as it is finished, so that at the end the M4B only needs its tags, chapter marks and cover (seconds, not
    minutes). One continuous encoding: chapters encoded apart and joined drift by the encoder's lead-in, about
    40 ms at every join. ffmpeg reads from a pipe of its own, never the worker's standard input."""

    def __init__(self, out: Path):
        self.out = out
        self.chapters: queue.Queue[Path | None] = queue.Queue()
        self.error: str | None = None
        self.log = out.with_suffix(".log")
        with open(self.log, "wb") as errors:
            self.process = subprocess.Popen(
                [ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "s16le", "-ar", str(RATE), "-ac", "1",
                 "-i", "pipe:0", "-c:a", "aac", "-b:a", BITRATE, "-f", "adts", str(out)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors, **LOW_PRIORITY)
        self.feeder = threading.Thread(target=self._feed, daemon=True)
        self.feeder.start()

    def add(self, chapter: Path) -> None:
        self.chapters.put(chapter)

    def _feed(self) -> None:
        stdin = self.process.stdin
        try:
            while (path := self.chapters.get()) is not None:
                with sf.SoundFile(str(path)) as audio:
                    for block in audio.blocks(blocksize=RATE * 60, dtype="int16"):
                        stdin.write(block.tobytes())
            stdin.close()
        except (OSError, ValueError, RuntimeError) as exc:  # ffmpeg stopped, or a chapter could not be read
            self.error = str(exc) or type(exc).__name__

    def finish(self, timeout: float) -> Path | None:
        """Wait until everything added is encoded: the AAC, or None if it is not complete."""
        self.chapters.put(None)
        self.feeder.join(timeout)
        if not self.feeder.is_alive():
            try:
                self.process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                pass
        if self.feeder.is_alive() or self.process.poll() != 0 or self.error:
            self.error = self.error or self.log.read_text(encoding="utf-8", errors="replace")[-300:] or "it did not finish in time"
            self.stop()
            return None
        return self.out

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()


def build_m4b(chapters: list[ChapterAudio], title: str, authors: list[str], language: str, cover: bytes | None, out: Path,
              encoded: Path | None = None) -> None:
    """Join the chapters into one M4B with chapter marks, tags and the cover picture. With `encoded` (the book's
    AAC, made while it was read) the audio is copied as it is; otherwise the chapters are encoded now."""
    work = out.parent
    meta = work / "metadata.txt"
    meta.write_text(ffmetadata(title, authors, chapters, language), encoding="utf-8")
    if encoded is not None:
        args = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(encoded), "-i", str(meta)]
        audio = ["-c:a", "copy"]
    else:
        listing = work / "chapters.txt"
        quoted = (c.path.resolve().as_posix().replace("'", "'\\''") for c in chapters)  # the concat list's quoting
        listing.write_text("".join(f"file '{q}'\n" for q in quoted), encoding="utf-8")
        args = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(meta)]
        audio = ["-c:a", "aac", "-b:a", BITRATE, "-ac", "1", "-ar", str(RATE)]
    maps = ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
    if cover:
        picture = work / ("cover.png" if cover[:4] == b"\x89PNG" else "cover.jpg")
        picture.write_bytes(cover)
        args += ["-i", str(picture)]
        maps += ["-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    tmp = out.with_suffix(".tmp.m4b")
    args += [*maps, *audio, "-movflags", "+faststart", "-f", "mp4", str(tmp)]
    # No standard input: ffmpeg looks there for key presses, and the worker's input is the local app's pipe, which
    # the worker itself waits on. On Windows ffmpeg then hung for good before finishing the file.
    # And a time limit, as the worker does not watch this step for stalls: four times faster than real time is
    # far slower than even the server's processor encodes.
    limit = 600 + sum(c.seconds for c in chapters) / 4
    try:
        done = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, check=False, creationflags=NO_WINDOW, timeout=limit)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg did not finish the audiobook file in {limit / 60:.0f} minutes") from None
    if done.returncode != 0:
        raise RuntimeError(f"ffmpeg could not build the audiobook: {done.stderr.decode(errors='replace')[-400:]}")
    tmp.replace(out)


def to_mp3(audio: np.ndarray, out: Path, rate: int = RATE, bitrate: str = "64k") -> None:
    fmt = ["-f", "f32le", "-ac", "1", "-ar", str(rate)]
    out.write_bytes(_pipe([*fmt, "-i", "pipe:0", "-b:a", bitrate, "-f", "mp3", "pipe:1"], audio.astype(np.float32).tobytes()))
