# Audiobooks

A finished book (a PDF converted here, or an EPUB uploaded and enriched) can be read aloud into an M4B
audiobook: one file with chapter marks, the cover and the title, which audiobook apps (Apple Books,
BookPlayer, Smart AudioBook Player, VLC) open as a book. Hebrew and English.

## Voices

| Engine | What it is | Speed on an RTX 2000 Ada laptop | License |
|---|---|---|---|
| **BlueTTS** (default) | Open-source model by the authors of Phonikud. Natural Hebrew; reads numbers, dates and English words inside Hebrew. Voices: Noa, Lily, Daniel, Adam. | "Smooth" about 2.5x faster than real time, "fast" about 5x, on the processor | Code MIT; the model files state no license |
| **Chatterbox** | Resemble AI's open multilingual model, on the GPU. Livelier, slower. Its voices copy BlueTTS's voices of the same name from a short recording. | about real time | MIT |
| **Microsoft Edge voices** | Microsoft's online read-aloud voices (Hila and Avri in Hebrew; Ava, Andrew, Sonia and Ryan in English). | many times faster than real time | Free, but an unofficial use of Microsoft's service that may change or stop |

A 200-page book is about 300,000 letters: roughly 7 hours of Hebrew audio (5 in English). With BlueTTS on
"smooth" it takes about 3 hours to make; with Chatterbox about 7 to 8.

Speed (80% to 120%) changes how fast the narrator reads; the dialog plays a sample of each voice at that speed.

## How it works

```
site (no GPU)                                              computer with a GPU
"Make audiobook" ─► audio job ─► the sandboxed worker
                                  reads the EPUB: chapters,
                                  paragraphs, language
                                  (narration.py, script.json)
                                        │
                                        ▼ waits for voices
        /api/voice  ◄── claim, text, progress, upload ──  pdf2epub-voice (voice-worker/)
                                        │                 reads each chapter, builds the M4B
                                        ▼
                               "Download audiobook"
```

* **The text** (`src/pdf2epub/narration.py`) comes from the EPUB, so converted and uploaded books are read
  the same way. Chapters follow the book's contents, including entries that point into the middle of a file.
  Left out: the contents page and front matter before the first chapter, footnotes and the marks pointing to
  them, page numbers, tables and pictures. A scene break becomes a pause. Each paragraph is read in its own
  language, so an English quotation in a Hebrew book is read in English.
* **The voice worker** (`voice-worker/`, its own Python 3.12 environment: Chatterbox and Docling need
  different versions of the same libraries) runs on any computer with the engines installed. It connects out
  to the site, so nothing at home has to be reachable. It reads one chapter at a time and keeps finished
  chapters, so a job interrupted by sleep or a restart continues where it stopped (the site gives such jobs
  ten attempts). It keeps Windows awake while it reads. The audiobook is mono AAC at 48 kbit/s (about 21 MB
  per hour), uploaded in parts of 45 MB because Cloudflare refuses larger requests.
* **Security.** The voice API uses its own token (`PDF2EPUB_VOICE_TOKEN`), not a browser session, and only
  the worker that claimed a job may report on it. The worker never sees the uploaded files, only the text to
  read and the cover. Without a token the API is off on the public site; the local app accepts the worker
  from the same computer without one.
* **Who may use it**: `PDF2EPUB_AUDIOBOOKS=admins` (the default) shows it to admins only; `everyone` opens it to
  everyone signed in with Google (never guests), with `PDF2EPUB_AUDIOBOOKS_PER_DAY` (default 1) and
  `PDF2EPUB_MAX_AUDIOBOOK_CHARS` (default 1,500,000 letters, about 30 hours); `off` hides it.

## Setting up the computer that makes the voices (Windows)

Needs [uv](https://docs.astral.sh/uv/), ffmpeg on PATH, and for Chatterbox an NVIDIA GPU with 8 GB.

1. On the server, create a token and add it to `deploy/secrets.env`, then restart the web container:

   ```bash
   echo "PDF2EPUB_VOICE_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" >> secrets.env
   docker compose up -d
   ```

2. On the GPU computer, from this repository:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install-voice-worker.ps1 -Url https://pdf2epub.danielkoryat.com
   ```

   It asks for the token, installs `voice-worker\` (a few GB the first time; models download on first use),
   and starts the worker now and at every sign-in. `-NoChatterbox` skips Chatterbox.

For the local app only, skip the token: `uv run --project voice-worker pdf2epub-voice run` serves
http://127.0.0.1:5050.

Useful commands (`uv run --project voice-worker pdf2epub-voice ...`):

* `check`: engines, GPU, ffmpeg, where the config and log are;
* `say "שלום" --engine blue --voice noa -o hello.mp3`: try a voice;
* `run --once`: read one waiting book and exit;
* `samples --out src/pdf2epub/web/static/voices`: make the voice samples the site plays.

The admin dashboard shows when the worker was last seen and which engines it has.
