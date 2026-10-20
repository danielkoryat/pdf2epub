# Audiobooks

A finished book (a PDF converted here, or an EPUB uploaded and enriched) can be read aloud into an M4B
audiobook: one file with chapter marks, the cover and the title, which audiobook apps (Apple Books,
BookPlayer, Smart AudioBook Player, VLC) open as a book. Hebrew and English.

## Voices

| Engine | What it is | Speed on a laptop (RTX 2000 Ada) | On the site's server (2 ARM cores) | License |
|---|---|---|---|---|
| **BlueTTS** (default) | Open-source model by the authors of Phonikud. Natural Hebrew; reads numbers, dates and English words inside Hebrew. Voices: Noa, Lily, Daniel, Adam. | about 2.5x faster than real time, on the processor | about half of real time | Code MIT; the model files state no license |
| **Chatterbox** | Resemble AI's open multilingual model, on the GPU. Livelier, slower. Its voices copy BlueTTS's voices of the same name from a short recording. | about real time | not offered (no GPU) | MIT |
| **Microsoft Edge voices** | Microsoft's online read-aloud voices (Hila and Avri in Hebrew; Ava, Andrew, Sonia and Ryan in English). | many times faster than real time | the same | Free, but an unofficial use of Microsoft's service that may change or stop |

A 200-page book is about 300,000 letters: roughly 7 hours of Hebrew audio (5 in English). With BlueTTS on
it takes about 3 hours to make on the laptop and about 13 on the server; with
Chatterbox on the laptop about 7 to 8. The voice worker measures its own speed on every chapter and the site
bases its estimates on that.

Speed (80% to 120%) changes how fast the narrator reads; the dialog plays a sample of each voice at that speed.

## How it works

```
web app                                                   voice worker
"Make audiobook" ─► audio job ─► the conversion worker
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

Where the voices are made:

* **The public site** makes them on its own server, in the `voice` container next to the web app
  (`deploy/compose.yaml`, image `voice-worker/Dockerfile`): BlueTTS and the Edge voices. The server has no
  GPU, so Chatterbox is not offered there.
* **The local app** (the desktop shortcut) makes them on your computer: it starts the voice worker when it
  starts and stops it when its window is closed, so Chatterbox can use the GPU. Nothing runs in the
  background when the app is closed.

* **The text** (`src/pdf2epub/narration.py`) comes from the EPUB, so converted and uploaded books are read
  the same way. Chapters follow the book's contents, including entries that point into the middle of a file.
  Left out: the contents page and front matter before the first chapter, footnotes and the marks pointing to
  them, page numbers, tables and pictures. A scene break becomes a pause. Each paragraph is read in its own
  language, so an English quotation in a Hebrew book is read in English.
* **The voice worker** (`voice-worker/`, its own Python 3.12 environment: Chatterbox and Docling need
  different versions of the same libraries) talks to the web app over its API. It reads one chapter at a time
  and keeps finished chapters, so a job interrupted by sleep or a restart continues where it stopped (the site
  gives such jobs ten attempts). It keeps Windows awake while it reads. The site's dialog only offers the
  engines a running voice worker has. The audiobook is mono AAC at 48 kbit/s (about 21 MB
  per hour), uploaded in parts of 45 MB because Cloudflare refuses larger requests.
* **Security.** The voice API uses its own token (`PDF2EPUB_VOICE_TOKEN`), not a browser session, and only
  the worker that claimed a job may report on it. The worker never sees the uploaded files, only the text to
  read and the cover; on the server its container has no access to the data volume and holds no other
  secret. Without a token the API is off on the public site; the local app accepts the worker from the same
  computer without one.
* **Who may use it**: `PDF2EPUB_AUDIOBOOKS=admins` (the default) shows it to admins only; `everyone` opens it to
  everyone signed in with Google (never guests), with `PDF2EPUB_AUDIOBOOKS_PER_DAY` (default 1) and
  `PDF2EPUB_MAX_AUDIOBOOK_CHARS` (default 1,500,000 letters, about 30 hours); `off` hides it.

## Setting up

**On the server** nothing extra: `deploy/deploy.sh` builds the voice image and copies
`PDF2EPUB_VOICE_TOKEN` from `secrets.env` into `voice.env`, the only setting the voice container gets. To
create the token:

```bash
echo "PDF2EPUB_VOICE_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" >> secrets.env
```

**For the local app (Windows)**: needs [uv](https://docs.astral.sh/uv/), ffmpeg on PATH, and for Chatterbox an
NVIDIA GPU with 8 GB. From this repository:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-voice-worker.ps1
```

It installs `voice-worker\` (a few GB the first time; models download on first use). `-NoChatterbox` skips
Chatterbox. From then on the desktop shortcut (or `pdf2epub-web`) starts the worker along with the app;
`pdf2epub-web --no-voice-worker` leaves it off.

Useful commands (`uv run --project voice-worker pdf2epub-voice ...`):

* `check`: engines, GPU, ffmpeg, where the config and log are;
* `say "שלום" --engine blue --voice noa -o hello.mp3`: try a voice;
* `run --once`: read one waiting book from the local app and exit;
* `samples --out src/pdf2epub/web/static/voices`: make the voice samples the site plays.

The admin dashboard shows when each worker was last seen and which engines it has.
