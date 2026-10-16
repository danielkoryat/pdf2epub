"""Sign-in with Google (OpenID Connect) and sessions.

Authlib validates Google's ID token (signature, issuer, audience, expiry, nonce); only verified e-mail addresses
are accepted. A session is a random token in an HttpOnly, SameSite=Lax cookie (Secure and __Host- prefixed over
HTTPS); the database stores only its SHA-256, and sessions can be revoked (sign-out, a blocked user).
In local mode there is no sign-in: every request belongs to the built-in local user, who is an admin.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from .db import Database
from .security import Client, RateLimiter, client_of
from .settings import Settings

LOCAL_USER: dict[str, Any] = {
    "id": "local", "email": "", "name": "You", "picture": "", "role": "admin", "status": "active", "token": "",
}


def cookie_name(settings: Settings) -> str:
    return "__Host-pdf2epub_session" if settings.secure_cookies else "pdf2epub_session"


class Auth:
    def __init__(self, settings: Settings, db: Database, limiter: RateLimiter):
        self.settings = settings
        self.db = db
        self.limiter = limiter
        self.oauth = None
        if settings.server:
            from authlib.integrations.starlette_client import OAuth

            self.oauth = OAuth()
            self.oauth.register(
                name="google",
                server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                client_id=settings.google_client_id,
                client_secret=settings.google_client_secret,
                client_kwargs={"scope": "openid email profile"},
            )

    # -- request helpers -----------------------------------------------------------------------------------

    def client(self, request: Request) -> Client:
        return client_of(request, self.settings)

    def optional_user(self, request: Request) -> dict[str, Any] | None:
        if not self.settings.server:
            return LOCAL_USER
        token = request.cookies.get(cookie_name(self.settings), "")
        if not token or len(token) > 200:
            return None
        session = self.db.session(token)
        if session is None or session["status"] != "active":
            return None
        if time.time() - session["last_seen"] > 300:
            self.db.touch_session(token)
        return {
            "id": session["user_id"], "email": session["email"], "name": session["name"], "picture": session["picture"],
            "role": session["role"], "status": session["status"], "token": token,
        }

    def user(self, request: Request) -> dict[str, Any]:
        user = self.optional_user(request)
        if user is None:
            raise HTTPException(401, "Please sign in")
        return user

    def admin(self, request: Request) -> dict[str, Any]:
        user = self.user(request)
        if user["role"] != "admin":
            self.audit(request, user, "admin.denied", "denied", target=request.url.path)
            raise HTTPException(404, "Not found")
        return user

    def audit(self, request: Request, user: dict[str, Any] | None, action: str, outcome: str = "ok", target: str = "", **details: Any) -> None:
        if not self.settings.server:
            return
        c = self.client(request)
        self.db.audit(
            action, outcome,
            user_id=(user or {}).get("id", ""), email=(user or {}).get("email", ""),
            ip=c.ip, country=c.country, user_agent=c.user_agent, target=target, details=details,
        )

    # -- the sign-in flow ----------------------------------------------------------------------------------

    async def login(self, request: Request) -> Response:
        c = self.client(request)
        ok, _wait = self.limiter.allow("auth", c.ip)
        if not ok:
            self.audit(request, None, "auth.rate_limited", "denied")
            raise HTTPException(429, "Too many sign-in attempts, try again later")
        redirect_uri = f"{self.settings.base_url}/auth/callback"
        return await self.oauth.google.authorize_redirect(request, redirect_uri)

    async def callback(self, request: Request) -> Response:
        from authlib.integrations.base_client.errors import OAuthError

        c = self.client(request)
        ok, _wait = self.limiter.allow("auth", c.ip)
        if not ok:
            raise HTTPException(429, "Too many sign-in attempts, try again later")
        try:
            token = await self.oauth.google.authorize_access_token(request)
        except OAuthError as exc:
            self.audit(request, None, "auth.login", "error", reason=exc.error)
            return RedirectResponse("/?signin=failed", status_code=303)
        info = token.get("userinfo") or {}
        if not info.get("sub") or not info.get("email") or not info.get("email_verified"):
            self.audit(request, None, "auth.login", "denied", reason="unverified email")
            return RedirectResponse("/?signin=unverified", status_code=303)
        email = str(info["email"]).lower()
        user = self.db.upsert_google_user(
            sub=str(info["sub"]), email=email, name=str(info.get("name") or email)[:200],
            picture=str(info.get("picture") or "")[:500], admin=email in self.settings.admin_emails,
            ip=c.ip, country=c.country,
        )
        if user["status"] != "active":
            self.audit(request, user, "auth.login", "denied", reason="blocked")
            return RedirectResponse("/?signin=blocked", status_code=303)
        session_token = secrets.token_urlsafe(32)
        self.db.create_session(session_token, user["id"], self.settings.session_days, c.ip, c.country, c.user_agent)
        self.audit(request, user, "auth.login")
        response = RedirectResponse("/", status_code=303)
        self._set_cookie(response, session_token)
        return response

    def logout(self, request: Request) -> Response:
        user = self.optional_user(request)
        if user and user.get("token"):
            self.db.revoke_session(user["token"])
            self.audit(request, user, "auth.logout")
        response = Response(status_code=204)
        response.delete_cookie(cookie_name(self.settings), path="/", secure=self.settings.secure_cookies, httponly=True, samesite="lax")
        return response

    def _set_cookie(self, response: Response, token: str) -> None:
        response.set_cookie(
            cookie_name(self.settings), token, max_age=self.settings.session_days * 86400, path="/",
            secure=self.settings.secure_cookies, httponly=True, samesite="lax",
        )
