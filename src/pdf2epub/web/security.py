"""Request-level protections: the client's address and country, rate limits, security headers, a same-origin
check on every state-changing request, and a request ID for tracing.
"""

from __future__ import annotations

import ipaddress
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .settings import Settings


@dataclass(frozen=True)
class Client:
    ip: str
    country: str
    user_agent: str


def client_of(request: Request, settings: Settings) -> Client:
    """The real client. Behind Cloudflare and Caddy the TCP peer is the proxy, so in that case the proxy's
    headers are used; otherwise they are ignored, because a client could forge them."""
    ip = request.client.host if request.client else ""
    country = ""
    if settings.trust_proxy:
        forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0]
        if forwarded.strip():
            ip = forwarded.strip()
        country = request.headers.get("cf-ipcountry", "")[:2].upper()
    try:
        ip = str(ipaddress.ip_address(ip))
    except ValueError:
        ip = ""
    if country in ("XX", "T1"):  # Cloudflare: unknown / Tor
        country = "" if country == "XX" else "TOR"
    return Client(ip=ip, country=country, user_agent=request.headers.get("user-agent", "")[:300])


class RateLimiter:
    """Sliding-window limits per key (an IP address or a user), kept in memory: the web app runs as one process."""

    # (requests, seconds) per kind of request
    RULES: dict[str, tuple[int, int]] = {
        "auth": (20, 600),       # sign-in attempts per IP
        "upload": (10, 3600),    # uploads per user
        "write": (60, 60),       # other changes per user
        "download": (60, 600),   # EPUB downloads per user
        "read": (600, 60),       # reads (polling the library) per user
        "anonymous": (120, 60),  # requests per IP before signing in
    }

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, kind: str, key: str) -> tuple[bool, int]:
        """(allowed, seconds to wait)."""
        limit, window = self.RULES[kind]
        now = time.monotonic()
        with self._lock:
            hits = self._hits[(kind, key)]
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                return False, int(hits[0] + window - now) + 1
            hits.append(now)
            if len(self._hits) > 50_000:  # forget idle keys
                for k in [k for k, v in self._hits.items() if not v][:10_000]:
                    del self._hits[k]
            return True, 0


def content_security_policy(settings: Settings) -> str:
    posthog = ""
    if settings.posthog_key:
        api = settings.posthog_host
        assets = api.replace(".i.posthog.com", "-assets.i.posthog.com")
        posthog = f" {api} {assets}"
    return (
        "default-src 'self'; "
        f"script-src 'self'{posthog}; "
        "style-src 'self' 'unsafe-inline'; "  # style attributes set by the page's own script
        "img-src 'self' data: blob: https://*.googleusercontent.com; "
        f"connect-src 'self'{posthog}; "
        "font-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://accounts.google.com"
    )


class SecurityMiddleware(BaseHTTPMiddleware):
    """Security headers, request IDs, and a same-origin check on requests that change something (a CSRF
    defence that doesn't need tokens: browsers always send Origin on such cross-site requests)."""

    SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    def __init__(self, app, settings: Settings):
        super().__init__(app)
        self.settings = settings
        self.csp = content_security_policy(settings)
        allowed = {settings.base_url}
        if not settings.server:  # local mode answers on 127.0.0.1 and localhost alike
            port = urlsplit(settings.base_url).port
            allowed |= {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        self.allowed_origins = allowed

    async def dispatch(self, request: Request, call_next) -> Response:
        request.state.request_id = secrets.token_hex(8)
        if request.method not in self.SAFE_METHODS and request.url.path.startswith(("/api/", "/auth/logout")):
            origin = request.headers.get("origin") or _origin_of(request.headers.get("referer", ""))
            # Browsers always say where a cross-site request comes from. With neither header the request is
            # not from a browser (a script on this computer): fine locally, refused on the public server.
            if (origin or self.settings.server) and origin not in self.allowed_origins:
                return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
        response = await call_next(request)
        h = response.headers
        h["X-Request-ID"] = request.state.request_id
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        h["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
        h["Content-Security-Policy"] = self.csp
        if self.settings.secure_cookies:
            h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.url.path.startswith("/api/"):
            h.setdefault("Cache-Control", "no-store")
        elif request.url.path.startswith("/static/"):
            h.setdefault("Cache-Control", "no-cache")  # revalidate (a cheap 304), so an update reaches everyone at once
        return response


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""
