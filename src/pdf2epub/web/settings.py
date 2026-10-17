"""Configuration of the web app, from environment variables.

Two modes:
  * local  (default): one person on their own computer. No sign-in, no limits, the conversion worker runs inside
    the web process. This is what the desktop shortcut starts.
  * server: a public website. Google sign-in, every user sees only their own books, page allowances and rate
    limits, an admin dashboard, an append-only audit log; the conversion worker runs as a separate, sandboxed
    process (pdf2epub-worker).
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .. import config


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return default if not value else value in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Limits:
    free_pages_per_month: int = 300  # pages a user may convert for free each calendar month
    max_upload_mb: int = 100
    max_pdf_pages: int = 600
    max_jobs_per_day: int = 10
    max_waiting_jobs: int = 3  # queued or running, per user
    max_storage_mb: int = 1024  # per user, uploads + results
    job_timeout_minutes: int = 90
    max_epub_entries: int = 5000  # files inside an uploaded EPUB
    max_epub_unpacked_mb: int = 500  # total unpacked size of an uploaded EPUB
    # Guests (no sign-in). Their allowance is counted per browser and per network address together, so a new
    # private window does not mean new pages: all guests from one IPv4 address (or IPv6 /64) share the pool.
    guest_pages_per_month: int = 60
    guest_jobs_per_day: int = 3  # per network address
    guest_max_storage_mb: int = 200
    guests_per_day: int = 5  # new guest libraries per network address per day

    @classmethod
    def from_env(cls) -> "Limits":
        defaults = cls()
        return cls(**{name: _int(f"PDF2EPUB_{name.upper()}", getattr(defaults, name)) for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Settings:
    mode: str = "local"
    data_dir: Path = field(default_factory=lambda: config.PROJECT_ROOT / "data")
    base_url: str = "http://127.0.0.1:5050"  # the public address, for sign-in redirects and same-origin checks
    session_secret: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    admin_emails: frozenset[str] = frozenset()
    session_days: int = 30
    trust_proxy: bool = False  # read the client address from Cloudflare / the reverse proxy
    worker_in_process: bool = True
    posthog_key: str = ""
    posthog_host: str = "https://eu.i.posthog.com"
    timezone: str = "UTC"  # what "today" means on the admin dashboard
    allow_guests: bool = True  # server mode: people may convert without signing in, within the guest limits
    turnstile_site_key: str = ""  # Cloudflare Turnstile: a bot check before a guest library is created
    turnstile_secret: str = ""
    limits: Limits = field(default_factory=Limits)

    @property
    def server(self) -> bool:
        return self.mode == "server"

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.startswith("https://")

    @classmethod
    def from_env(cls, web: bool = True, **overrides) -> "Settings":
        """Settings from the environment. web=False is for the worker, which needs no secrets: it never sees
        the session secret or the Google credentials (and must not, as it is the part that opens hostile files)."""
        mode = os.environ.get("PDF2EPUB_MODE", "local").strip().lower()
        if mode not in ("local", "server"):
            raise ValueError("PDF2EPUB_MODE must be 'local' or 'server'")
        server = mode == "server"
        values = dict(
            mode=mode,
            data_dir=Path(os.environ.get("PDF2EPUB_DATA", config.PROJECT_ROOT / "data")),
            base_url=os.environ.get("PDF2EPUB_BASE_URL", "http://127.0.0.1:5050").rstrip("/"),
            session_secret=os.environ.get("PDF2EPUB_SESSION_SECRET", ""),
            google_client_id=os.environ.get("PDF2EPUB_GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.environ.get("PDF2EPUB_GOOGLE_CLIENT_SECRET", ""),
            admin_emails=frozenset(e.strip().lower() for e in os.environ.get("PDF2EPUB_ADMIN_EMAILS", "").split(",") if e.strip()),
            session_days=_int("PDF2EPUB_SESSION_DAYS", 30),
            trust_proxy=_bool("PDF2EPUB_TRUST_PROXY", server),
            worker_in_process=_bool("PDF2EPUB_WORKER_IN_PROCESS", not server),
            posthog_key=os.environ.get("PDF2EPUB_POSTHOG_KEY", ""),
            posthog_host=os.environ.get("PDF2EPUB_POSTHOG_HOST", "https://eu.i.posthog.com").rstrip("/"),
            timezone=os.environ.get("PDF2EPUB_TIMEZONE", "UTC"),
            allow_guests=_bool("PDF2EPUB_ALLOW_GUESTS", True),
            turnstile_site_key=os.environ.get("PDF2EPUB_TURNSTILE_SITE_KEY", "").strip(),
            turnstile_secret=os.environ.get("PDF2EPUB_TURNSTILE_SECRET", "").strip(),
            limits=Limits.from_env(),
        )
        values.update(overrides)
        settings = cls(**values)
        if web:
            settings.validate()
        return settings

    def validate(self) -> None:
        if not self.server:
            return
        missing = [name for name, value in (
            ("PDF2EPUB_SESSION_SECRET", self.session_secret),
            ("PDF2EPUB_GOOGLE_CLIENT_ID", self.google_client_id),
            ("PDF2EPUB_GOOGLE_CLIENT_SECRET", self.google_client_secret),
            ("PDF2EPUB_ADMIN_EMAILS", self.admin_emails),
        ) if not value]
        if missing:
            raise ValueError(f"server mode needs: {', '.join(missing)}")
        if len(self.session_secret) < 32:
            raise ValueError("PDF2EPUB_SESSION_SECRET must be at least 32 characters (use: python -c \"import secrets; print(secrets.token_urlsafe(48))\")")
        if not self.base_url.startswith("https://"):
            raise ValueError("server mode must be served over https (PDF2EPUB_BASE_URL)")

    def effective_session_secret(self) -> str:
        # Local mode has no sign-in; the secret only signs the short-lived OAuth state, so a random one is fine.
        return self.session_secret or secrets.token_urlsafe(48)
