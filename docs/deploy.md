# Running pdf2epub as a website

The web app has two modes. **Local mode** (the default) is for one person on their own computer: no sign-in,
no limits. **Server mode** turns it into a public site:

- Google sign-in, and every user sees only their own books
- a monthly page allowance per user, plus pages an admin grants
- audiobooks, read aloud by a voice worker container on the same server (see [audiobooks.md](audiobooks.md))
- rate limits, size, page and storage limits, and checks that refuse hostile files
- conversions in a sandboxed worker container with no network and no secrets
- an admin dashboard (users, sign-ins with address and country, usage, jobs) and an append-only, tamper-evident audit log

This guide sets it up with Docker on a Linux server behind Caddy and Cloudflare. The files are in `deploy/`.

## What runs where

```
visitor ── Cloudflare ── Caddy (TLS, :443) ── web container (127.0.0.1:5250) ──┐
                                                    ▲                         data volume (SQLite + books)
                                                    │ voice API               │
                                              voice container   worker container (no network)
```

- **web** signs people in, takes uploads and serves the finished books. It holds the secrets.
- **worker** takes jobs from the queue in the database and runs each in its own child process, with a time
  limit. It is the only part that opens uploaded files, so it gets nothing else: no network interface, no
  secrets, a read-only filesystem apart from the data volume, no Linux capabilities, and memory, CPU and
  process caps.
- Both containers use the same image and run as an unprivileged user.
- **voice** reads finished books aloud into audiobooks, with BlueTTS on the processor and Microsoft's online
  Edge voices (Chatterbox needs a GPU and is left out). It has its own image (`voice-worker/Dockerfile`) and
  volume, never sees the data volume or the uploaded files, and only talks to the web container's voice API,
  signing its requests with `PDF2EPUB_VOICE_TOKEN`, and to Microsoft for the Edge voices; see
  [audiobooks.md](audiobooks.md). An audiobook takes about 21 MB per hour of audio on the data volume.

The image works on x86-64 and ARM64 (for example Oracle Cloud's Ampere machines). It needs no GPU. On two ARM
cores a 300-page scanned book takes roughly 35 to 45 minutes; born-digital PDFs are much faster.

## 1. Google sign-in credentials

1. Open <https://console.cloud.google.com/>, create a project (for example "pdf2epub").
2. **APIs & Services → OAuth consent screen** (called "Google Auth Platform" in newer consoles):
   - User type **External**. App name, support e-mail, and the developer contact e-mail.
   - Authorized domain: `danielkoryat.com`.
   - Scopes: only `openid`, `email` and `profile` (these need no Google review).
   - **Publish** the app (status "In production"). While it is in "Testing", only the test users you list can sign in.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type **Web application**.
   - Authorized JavaScript origins: `https://pdf2epub.danielkoryat.com`
   - Authorized redirect URIs: `https://pdf2epub.danielkoryat.com/auth/callback`
4. Copy the client ID and client secret into `deploy/secrets.env` (step 3).

## 2. DNS and Cloudflare

1. In Cloudflare, add an `A` record `pdf2epub` → the server's public IP address, **Proxied** (orange cloud).
2. **SSL/TLS → Overview**: encryption mode **Full (strict)**. Caddy gets a real certificate for the origin.
3. Optional but useful: **Security → WAF → Rate limiting rules** for `/auth/` and `/api/jobs` as a first line in
   front of the app's own limits, and **Bot Fight Mode**.

Caddy only accepts connections from Cloudflare's addresses (see `deploy/Caddyfile`), because the app trusts
the visitor address and country that Cloudflare sends. Nobody can reach the site by the server's IP directly.

## 3. Configure

On the server:

```sh
git clone https://github.com/danielkoryat/pdf2epub.git /opt/pdf2epub
cd /opt/pdf2epub/deploy
cp settings.env.example settings.env      # base URL, time zone, limits (nothing secret)
cp secrets.env.example secrets.env        # session secret, Google client, admin e-mails
chmod 600 secrets.env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # paste as PDF2EPUB_SESSION_SECRET
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # and again as PDF2EPUB_VOICE_TOKEN (audiobooks)
```

`settings.env` is read by the web and worker containers, `secrets.env` only by the web container. Never put a
secret in `settings.env`. The voice container gets `voice.env`, which holds the voice token alone and is copied
from `secrets.env` (by `deploy.sh`, or the command in the next step).

## 4. Start

```sh
cd /opt/pdf2epub/deploy
(umask 077 && grep '^PDF2EPUB_VOICE_TOKEN=' secrets.env > voice.env)
docker compose up -d --build     # the first build downloads the models: about 15 minutes
docker compose ps                # web should become "healthy"
docker compose logs -f
```

Add the site from `deploy/Caddyfile` to `/etc/caddy/Caddyfile`, then:

```sh
sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

Open the site and sign in with an address from `PDF2EPUB_ADMIN_EMAILS`: the **Dashboard** button appears in
the header.

## Updating

```sh
/opt/pdf2epub/deploy/deploy.sh   # pull main, rebuild, restart, wait until healthy
```

### Automatic deploys

`.github/workflows/deploy.yml` runs the tests on every push; when a push to `main` passes, GitHub Actions
connects to the server with a key of its own and runs `deploy/deploy.sh`. The key is limited on the server to
that one script, so a leaked key can only redeploy what is already on `main`:

```sh
ssh-keygen -t ed25519 -N "" -C "github-deploy" -f deploy_key        # on any computer
# on the server, one line in ~/.ssh/authorized_keys:
#   command="/opt/pdf2epub/deploy/deploy.sh",restrict ssh-ed25519 AAAA... github-deploy
ssh-keyscan -t ed25519 <server address>                              # the host key line
```

Then add the repository secrets `DEPLOY_HOST` (`ubuntu@<server address>`; a secret because Cloudflare keeps
the address private), `DEPLOY_SSH_KEY` (the private key file) and `DEPLOY_KNOWN_HOSTS` (the `ssh-keyscan`
line), and delete the local copy of the private key. Without the secrets the workflow only tests.

The database is migrated automatically on start. A job that was running when the worker stopped is picked
up again a few minutes later (or failed, with its pages refunded, after repeated attempts).

## Backups

The database holds the users, books, page ledger and audit log. A consistent copy, safe while the site runs:

```sh
docker compose exec web pdf2epub-backup --keep 14     # writes /data/backups/pdf2epub-<time>.db.gz
```

Run it daily with a systemd timer (`/etc/systemd/system/pdf2epub-backup.service` and `.timer`):

```ini
# pdf2epub-backup.service
[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=/opt/pdf2epub/deploy
ExecStart=/usr/bin/docker compose exec -T web pdf2epub-backup --keep 14

# pdf2epub-backup.timer
[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

Then `sudo systemctl enable --now pdf2epub-backup.timer`.

The books' files are in the same volume (`/data/jobs`). Copy the volume off the server regularly, for example
with Oracle Cloud's scheduled boot-volume backups, or `rclone` to object storage. A backup that lives only on
the same disk does not survive the disk.

## Limits

All limits are environment variables in `settings.env` (`PDF2EPUB_<NAME>`). The defaults:

| Setting | Default | |
|---|---|---|
| `FREE_PAGES_PER_MONTH` | 300 | free pages per user per calendar month; admins can grant more |
| `MAX_UPLOAD_MB` | 100 | keep it at 95 or less behind Cloudflare's free plan (100 MB request limit) |
| `MAX_PDF_PAGES` | 600 | longest PDF accepted |
| `MAX_JOBS_PER_DAY` | 10 | uploads per user per day |
| `MAX_WAITING_JOBS` | 3 | queued or running jobs per user |
| `MAX_STORAGE_MB` | 1024 | uploads and results per user |
| `JOB_TIMEOUT_MINUTES` | 90 | a job running longer is stopped and refunded |
| `MAX_EPUB_ENTRIES` | 5000 | files inside an uploaded EPUB |
| `MAX_EPUB_UNPACKED_MB` | 500 | total unpacked size of an uploaded EPUB |

| `AUDIOBOOKS_PER_DAY` | 1 | audiobooks per user per day (when `PDF2EPUB_AUDIOBOOKS=everyone`) |
| `MAX_AUDIOBOOK_CHARS` | 1500000 | longest book read aloud, in letters (about 30 hours) |

`PDF2EPUB_AUDIOBOOKS` decides who sees audiobooks: `admins` (the default), `everyone` (signed in with Google;
never guests) or `off`.

Request rate limits (sign-in, uploads, other writes, downloads, reads) are built in. Admins have no page,
upload-count or storage limits (only the upload size).

### Guests

Unless `PDF2EPUB_ALLOW_GUESTS=false`, people can convert without signing in. "Try it without signing in"
creates a guest library tied to a cookie. Guests are counted per browser **and** per network (the IPv4 address,
or the /64 of an IPv6 address), so a private window or cleared cookies don't bring new pages:

| Setting | Default | |
|---|---|---|
| `GUEST_PAGES_PER_MONTH` | 60 | free pages per month, for one guest and for all guests of a network together |
| `GUEST_JOBS_PER_DAY` | 3 | uploads per network per day |
| `GUEST_MAX_STORAGE_MB` | 200 | per guest library |
| `GUESTS_PER_DAY` | 5 | new guest libraries per network per day |

With `PDF2EPUB_TURNSTILE_SITE_KEY` and `PDF2EPUB_TURNSTILE_SECRET` set (Cloudflare dashboard → Turnstile → Add
widget), a browser check runs before a guest library is created. When a guest signs in with Google, their
books move into the account.

## Security notes

- Sessions are random tokens stored hashed; the cookie is `__Host-`, `HttpOnly`, `Secure`, `SameSite=Lax`.
  Every write must come from the site's own origin.
- Only Google accounts with a verified e-mail can sign in. Blocking a user ends their sessions at once.
- Uploads are identified by their content, not their name. EPUBs are checked for zip bombs, unsafe paths,
  links, encryption and excessive size before anything unpacks them. Drafts are stored as JSON, never pickle.
- Admins see usage, not books: an admin cannot open other users' files either.
- Every sign-in, upload, download, deletion and admin action, and every request refused by a rate limit or
  an admin check, is written to the audit log. Each entry carries a hash of the previous one, and the
  database refuses changes and deletions, so tampering shows up in **Dashboard → Audit log → Check integrity**.
