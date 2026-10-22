"""pdf2epub-voice: run the voice worker, make voice samples, check the setup, or read a sentence aloud.

  pdf2epub-voice run [--url URL] [--token TOKEN] [--once]   take audiobook jobs from a pdf2epub site
  pdf2epub-voice check                                      engines, GPU and ffmpeg on this computer
  pdf2epub-voice samples --out DIR                          the short voice samples the site plays
  pdf2epub-voice say --engine blue --voice noa --lang he "שלום" -o out.mp3

The site and token come from the command line, else PDF2EPUB_VOICE_URL / PDF2EPUB_VOICE_TOKEN, else the config
file (%APPDATA%\\pdf2epub-voice\\config.json on Windows, ~/.config/pdf2epub-voice/config.json elsewhere). The local
app starts `run` itself; the server runs it in the voice container (Dockerfile).
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .audio import NO_WINDOW

log = logging.getLogger("pdf2epub_voice")

SAMPLE_TEXT = {
    "he": "שלום, אני אקרא לך את הספר. נתחיל בפרק הראשון, ונמשיך יחד עד הסוף.",
    "en": "Hello, I will read this book to you. We will begin with the first chapter, and go on together to the end.",
}


def config_dir() -> Path:
    base = os.environ.get("APPDATA") if os.name == "nt" else os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base or Path.home()) / "pdf2epub-voice"


def data_dir() -> Path:
    """Work files and the log: PDF2EPUB_VOICE_DATA, else the user's application data folder."""
    if os.environ.get("PDF2EPUB_VOICE_DATA"):
        path = Path(os.environ["PDF2EPUB_VOICE_DATA"])
    else:
        base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
        path = Path(base or Path.home()) / "pdf2epub-voice"
    path.mkdir(parents=True, exist_ok=True)
    return path


def models_dir() -> Path:
    """The voice models: PDF2EPUB_VOICE_MODELS (the server image keeps them in the image), else in data_dir()."""
    return Path(os.environ.get("PDF2EPUB_VOICE_MODELS") or data_dir() / "models")


def load_config() -> dict:
    path = config_dir() / "config.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _logging(verbose: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()] if sys.stderr else []  # none without a console
    handlers.append(logging.handlers.RotatingFileHandler(data_dir() / "worker.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def _gpu() -> str:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True, timeout=10,
                             creationflags=NO_WINDOW)
        return out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def cmd_run(args: argparse.Namespace) -> int:
    from .client import AuthError, Client, Gone
    from .engines import Engines, installed
    from .keepawake import keep_awake
    from .reader import Reader, Speeds

    if args.with_app:
        threading.Thread(target=_exit_with_app, daemon=True).start()
    cfg = load_config()
    url = args.url or os.environ.get("PDF2EPUB_VOICE_URL") or cfg.get("url") or "http://127.0.0.1:5050"
    token = args.token or os.environ.get("PDF2EPUB_VOICE_TOKEN") or cfg.get("token") or ""
    name = args.name or cfg.get("name") or socket.gethostname()
    available = installed()
    wanted = args.engines.split(",") if args.engines else cfg.get("engines") or available
    engines = [e for e in wanted if e in available]
    if not engines:
        log.error("No voice engine is installed (found: %s)", ", ".join(available) or "none")
        return 2
    client = Client(url, token, name)
    speeds = Speeds(data_dir() / "speeds.json")
    reader = Reader(client, Engines(models_dir(), available), data_dir() / "jobs", speeds)  # Chatterbox may need BlueTTS
    info = {"version": __version__, "gpu": _gpu()}
    log.info("Voice worker %s for %s, engines: %s", name, url, ", ".join(engines))
    while True:
        try:
            job = client.claim(engines, {**info, "speeds": speeds.ratios()})
        except AuthError as exc:
            log.error("%s: check the token", exc)
            return 2
        except Exception as exc:  # noqa: BLE001 -- offline, site down: try again later
            log.warning("Cannot reach %s: %s", url, exc)
            job = None
        if job is None:
            if args.once:
                return 0
            time.sleep(args.poll)
            continue
        log.info("Job %s: %s", job["id"], job["title"])
        try:
            with keep_awake():
                reader.read(job)
        except Gone:
            log.info("Job %s is gone", job["id"])
        except Exception as exc:  # noqa: BLE001 -- the engine failed on this book: tell the site
            log.exception("Job %s failed", job["id"])
            try:
                client.fail(job["id"], f"The voices could not be made: {exc}")
            except Exception:  # noqa: BLE001 -- the site will notice the silence and queue the job again
                log.warning("Could not report the failure")
        if args.once:
            return 0


def _exit_with_app() -> None:
    """Started by the local app, which holds this worker's standard input open while it runs: when the app stops
    in any way (its window closed, a crash), the input ends and the worker stops with it."""
    try:
        sys.stdin.buffer.read()
    except (OSError, ValueError, AttributeError):
        pass
    log.info("The app has stopped; so does the voice worker")
    os._exit(0)  # chapters already read are kept, and the job goes back to the queue on the site


def cmd_check(args: argparse.Namespace) -> int:
    from .audio import ffmpeg
    from .engines import installed

    print(f"pdf2epub-voice {__version__}, Python {sys.version.split()[0]}")
    print(f"Engines installed: {', '.join(installed()) or 'none'}")
    print(f"GPU: {_gpu() or 'none found'}")
    if "blue" in installed():
        import onnxruntime

        from .gpu import providers

        where = "the GPU" if providers() else "the processor"
        print(f"BlueTTS runs on {where} (ONNX Runtime {onnxruntime.__version__})")
    try:
        print(f"ffmpeg: {ffmpeg()}")
    except RuntimeError as exc:
        print(f"ffmpeg: {exc}")
    cfg = load_config()
    print(f"Site: {cfg.get('url') or os.environ.get('PDF2EPUB_VOICE_URL') or 'http://127.0.0.1:5050 (default)'}")
    print(f"Config: {config_dir() / 'config.json'}")
    print(f"Models: {models_dir()}")
    print(f"Work files and log: {data_dir()}")
    return 0


def cmd_samples(args: argparse.Namespace) -> int:
    from .audio import to_mp3
    from .engines import VOICES, Engines, installed

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    names = args.engines.split(",") if args.engines else installed()
    engines = Engines(models_dir(), names)
    for name in names:
        engine = engines.get(name)
        for lang, voices in VOICES[name].items():
            for voice in voices:
                path = out / f"{name}-{voice}-{lang}.mp3"
                if path.exists() and not args.force:
                    continue
                quality = "smooth" if name == "blue" else "standard"
                to_mp3(engine.synthesize(SAMPLE_TEXT[lang], lang, voice, 1.0, quality), path)
                print(path)
    return 0


def cmd_say(args: argparse.Namespace) -> int:
    from .audio import to_mp3
    from .engines import Engines, installed

    engines = Engines(models_dir(), installed())  # Chatterbox copies its voices from BlueTTS
    started = time.monotonic()
    audio = engines.get(args.engine).synthesize(args.text, args.lang, args.voice, args.speed, args.quality)
    to_mp3(audio, Path(args.output))
    print(f"{args.output}: {len(audio) / 24000:.1f} s of audio in {time.monotonic() - started:.1f} s")
    return 0


def main(argv: list[str] | None = None) -> int:
    # BlueTTS reads its files with the default encoding, which on Windows is not UTF-8: run in UTF-8 mode.
    if os.name == "nt" and not sys.flags.utf8_mode:
        return subprocess.call([sys.executable, "-X", "utf8", "-m", "pdf2epub_voice", *(argv if argv is not None else sys.argv[1:])],
                               creationflags=NO_WINDOW if not sys.stderr else 0)
    os.environ.setdefault("TQDM_DISABLE", "1")  # no progress bars from the models in the log
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    # Started at sign-in without a console (pythonw) there is no stdout or stderr, and libraries that print
    # (download progress bars, BlueTTS's notes) would fail: give them somewhere to write.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 -- for the life of the process
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    p = argparse.ArgumentParser(prog="pdf2epub-voice", description="Make the voices of pdf2epub audiobooks.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="take audiobook jobs from a pdf2epub site")
    run.add_argument("--url", help="the site (default: config, else http://127.0.0.1:5050)")
    run.add_argument("--token", help="the site's PDF2EPUB_VOICE_TOKEN")
    run.add_argument("--name", help="this computer's name on the site (default: the host name)")
    run.add_argument("--engines", help="comma-separated engines to offer (default: all installed)")
    run.add_argument("--poll", type=float, default=15.0, help="seconds between looks for new jobs (default: 15)")
    run.add_argument("--once", action="store_true", help="read at most one book, then exit")
    run.add_argument("--with-app", action="store_true", help="exit when standard input closes (how the local app starts it)")
    sub.add_parser("check", help="show what is installed")
    samples = sub.add_parser("samples", help="make the voice samples the site plays")
    samples.add_argument("--out", required=True)
    samples.add_argument("--engines")
    samples.add_argument("--force", action="store_true", help="make samples that already exist again")
    say = sub.add_parser("say", help="read a text aloud into an MP3")
    say.add_argument("text")
    say.add_argument("--engine", default="blue")
    say.add_argument("--voice", default="noa")
    say.add_argument("--lang", default="he")
    say.add_argument("--speed", type=float, default=1.0)
    say.add_argument("--quality", default="smooth")
    say.add_argument("-o", "--output", default="say.mp3")
    args = p.parse_args(argv)
    _logging(args.verbose)
    from .gpu import prefer_gpu_runtime

    prefer_gpu_runtime()
    return {"run": cmd_run, "check": cmd_check, "samples": cmd_samples, "say": cmd_say}[args.command](args)
