"""The admin dashboard's API: who uses the app, from where, how much, and the audit log.

Everything comes from the app's own database (users, sessions, jobs, the page ledger, the audit log), so the
numbers are exact. Every change an admin makes is itself written to the audit log.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .auth import Auth
from .db import Database
from .jobs import Library
from .quota import Quota, month_start
from .settings import Settings


class GrantBody(BaseModel):
    pages: int = Field(ge=1, le=1_000_000)
    note: str = Field(default="", max_length=200)


class AllowanceBody(BaseModel):
    monthly_pages: int | None = Field(default=None, ge=0, le=1_000_000)


def day_start(tz: ZoneInfo, days_ago: int = 0) -> float:
    now = datetime.now(tz)
    start = datetime(now.year, now.month, now.day, tzinfo=tz) - timedelta(days=days_ago)
    return start.timestamp()


def router(settings: Settings, db: Database, library: Library, auth: Auth, quota: Quota) -> APIRouter:
    r = APIRouter(prefix="/api/admin")
    tz = ZoneInfo(settings.timezone)

    def admin(request: Request) -> dict:
        return auth.admin(request)

    @r.get("/overview")
    def overview(_: dict = Depends(admin)) -> dict[str, Any]:
        today = day_start(tz)
        n = lambda sql, *p: db.one(sql, p)["n"]  # noqa: E731
        active_today = n(
            "SELECT COUNT(DISTINCT u) AS n FROM (SELECT user_id AS u FROM sessions WHERE last_seen>=? "
            "UNION SELECT owner_id AS u FROM jobs WHERE created>=?)", today, today,
        )
        stats = {
            "users_total": n("SELECT COUNT(*) AS n FROM users"),
            "users_new_today": n("SELECT COUNT(*) AS n FROM users WHERE created>=?", today),
            "users_active_today": active_today,
            "users_blocked": n("SELECT COUNT(*) AS n FROM users WHERE status='blocked'"),
            "signins_today": n("SELECT COUNT(*) AS n FROM audit WHERE action='auth.login' AND outcome='ok' AND ts>=?", today),
            "failed_signins_today": n("SELECT COUNT(*) AS n FROM audit WHERE action='auth.login' AND outcome!='ok' AND ts>=?", today),
            "books_today": n("SELECT COUNT(*) AS n FROM jobs WHERE created>=?", today),
            "books_total": n("SELECT COUNT(*) AS n FROM jobs"),
            "pages_today": n("SELECT COALESCE(SUM(pages),0) AS n FROM usage WHERE kind='charge' AND created>=?", today)
            - n("SELECT COALESCE(SUM(pages),0) AS n FROM usage WHERE kind='refund' AND created>=?", today),
            "pages_total": n("SELECT COALESCE(SUM(pages),0) AS n FROM jobs WHERE kind='pdf' AND status='done'"),
            "failed_today": n("SELECT COUNT(*) AS n FROM jobs WHERE status='failed' AND updated>=?", today),
            "queued": n("SELECT COUNT(*) AS n FROM jobs WHERE status='queued' AND pending IS NOT NULL"),
            "running": n("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('preparing','building')"),
            "storage_bytes": n("SELECT COALESCE(SUM(size),0) AS n FROM jobs"),
            "blocked_requests_today": n("SELECT COUNT(*) AS n FROM audit WHERE outcome='denied' AND ts>=?", today),
        }
        # Daily activity for the last 30 days.
        days = []
        for ago in range(29, -1, -1):
            start, end = day_start(tz, ago), day_start(tz, ago - 1) if ago else time.time() + 1
            days.append({
                "date": datetime.fromtimestamp(start, tz).strftime("%Y-%m-%d"),
                "active_users": n(
                    "SELECT COUNT(DISTINCT u) AS n FROM (SELECT user_id AS u FROM audit WHERE action='auth.login' AND outcome='ok' AND ts>=? AND ts<? "
                    "UNION SELECT owner_id AS u FROM jobs WHERE created>=? AND created<?)", start, end, start, end,
                ),
                "signins": n("SELECT COUNT(*) AS n FROM audit WHERE action='auth.login' AND outcome='ok' AND ts>=? AND ts<?", start, end),
                "new_users": n("SELECT COUNT(*) AS n FROM users WHERE created>=? AND created<?", start, end),
                "books": n("SELECT COUNT(*) AS n FROM jobs WHERE created>=? AND created<?", start, end),
                "pages": n("SELECT COALESCE(SUM(CASE kind WHEN 'charge' THEN pages WHEN 'refund' THEN -pages ELSE 0 END),0) AS n FROM usage WHERE created>=? AND created<?", start, end),
            })
        # Where users sign in from (distinct users per country, last 30 days).
        rows = db.query("SELECT DISTINCT user_id, country FROM audit WHERE action='auth.login' AND outcome='ok' AND ts>=?", (day_start(tz, 29),))
        countries = Counter(r["country"] or "unknown" for r in rows)
        voice_workers = [{"name": w["name"].removeprefix("voice:"), "last_seen": w["last_seen"], "engines": w["engines"]} for w in db.voice_workers()]
        return {"stats": stats, "days": days, "countries": countries.most_common(), "timezone": settings.timezone,
                "voice_workers": voice_workers}

    @r.get("/users")
    def users(q: str = "", _: dict = Depends(admin)) -> list[dict[str, Any]]:
        like = f"%{q.strip().lower()}%"
        rows = db.query(
            "SELECT u.*, "
            " (SELECT COUNT(*) FROM jobs j WHERE j.owner_id=u.id) AS books,"
            " (SELECT COALESCE(SUM(size),0) FROM jobs j WHERE j.owner_id=u.id) AS storage,"
            " (SELECT COUNT(*) FROM audit a WHERE a.user_id=u.id AND a.action='auth.login' AND a.outcome='ok') AS signins"
            " FROM users u WHERE lower(u.email) LIKE ? OR lower(u.name) LIKE ? ORDER BY u.last_login DESC LIMIT 500",
            (like, like),
        )
        since = month_start()
        for row in rows:
            row.pop("google_sub", None)
            row["balance"] = quota.balance(row["id"]).as_dict()
            row["pages_month"] = sum(
                (1 if u["kind"] == "charge" else -1) * u["pages"]
                for u in db.usage_rows(row["id"], since) if u["kind"] in ("charge", "refund")
            )
        return rows

    @r.get("/signins")
    def signins(limit: int = 200, _: dict = Depends(admin)) -> list[dict[str, Any]]:
        return db.query(
            "SELECT a.ts, a.user_id, a.email, a.ip, a.country, a.user_agent, a.outcome, a.details FROM audit a"
            " WHERE a.action='auth.login' ORDER BY a.id DESC LIMIT ?",
            (min(max(limit, 1), 1000),),
        )

    @r.get("/audit")
    def audit(action: str = "", user: str = "", outcome: str = "", before: int | None = None, _: dict = Depends(admin)) -> list[dict[str, Any]]:
        return db.audit_rows(action=action, user_id=user, outcome=outcome, before_id=before, limit=200)

    @r.get("/audit/verify")
    def verify(_: dict = Depends(admin)) -> dict[str, Any]:
        ok, count = db.verify_audit_chain()
        return {"intact": ok, "entries": count}

    @r.get("/jobs")
    def jobs(_: dict = Depends(admin)) -> list[dict[str, Any]]:
        rows = db.query(
            "SELECT j.id, j.kind, j.status, j.stage, j.done, j.total, j.pages, j.size, j.created, j.updated, j.worker, j.error,"
            " u.email FROM jobs j LEFT JOIN users u ON u.id=j.owner_id ORDER BY j.created DESC LIMIT 300"
        )
        return rows

    def change_user(request: Request, admin_user: dict, user_id: str, action: str, details: dict | None = None, **fields) -> dict:
        target = db.user(user_id)
        if target is None:
            raise HTTPException(404, "No such user")
        if fields:
            db.set_user(user_id, **fields)
        auth.audit(request, admin_user, action, target=target["email"], **{**fields, **(details or {})})
        return db.user(user_id)

    @r.post("/users/{user_id}/block")
    def block(user_id: str, request: Request, admin_user: dict = Depends(admin)) -> dict:
        if user_id == admin_user["id"]:
            raise HTTPException(400, "You cannot block yourself")
        user = change_user(request, admin_user, user_id, "admin.block", status="blocked")
        db.revoke_user_sessions(user_id)
        return user

    @r.post("/users/{user_id}/unblock")
    def unblock(user_id: str, request: Request, admin_user: dict = Depends(admin)) -> dict:
        return change_user(request, admin_user, user_id, "admin.unblock", status="active")

    @r.post("/users/{user_id}/grant")
    def grant(user_id: str, body: GrantBody, request: Request, admin_user: dict = Depends(admin)) -> dict:
        user = change_user(request, admin_user, user_id, "admin.grant", details={"pages": body.pages})
        quota.grant(user_id, body.pages, body.note or f"granted by {admin_user['email']}")
        return {**user, "balance": quota.balance(user_id).as_dict()}

    @r.post("/users/{user_id}/allowance")
    def allowance(user_id: str, body: AllowanceBody, request: Request, admin_user: dict = Depends(admin)) -> dict:
        user = change_user(request, admin_user, user_id, "admin.allowance", monthly_pages=body.monthly_pages)
        return {**user, "balance": quota.balance(user_id).as_dict()}

    return r
