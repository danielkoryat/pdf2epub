"""Page allowances: every user may convert a number of PDF pages for free each calendar month; beyond that they
use paid pages (bought, or granted by an admin). Pages are charged when a conversion starts and refunded if it
fails. EPUB enrichment and rebuilding a book after editing chapters are free.

Admins are not limited (their pages are still counted, for the statistics). Guests have a smaller allowance
that is also capped by what all guests from the same network used this month.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import Database
from .settings import Limits

UNLIMITED = 10**9


def month_start(now: float | None = None) -> float:
    t = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
    return datetime(t.year, t.month, 1, tzinfo=timezone.utc).timestamp()


@dataclass(frozen=True)
class Balance:
    free_allowance: int
    free_used: int
    paid: int
    unlimited: bool = False
    guest: bool = False
    network_left: int | None = None  # guests: what their network has left this month

    @property
    def free_left(self) -> int:
        left = max(self.free_allowance - self.free_used, 0)
        return left if self.network_left is None else min(left, self.network_left)

    @property
    def available(self) -> int:
        return UNLIMITED if self.unlimited else self.free_left + self.paid

    def as_dict(self) -> dict[str, int | bool]:
        return {"free_allowance": self.free_allowance, "free_used": self.free_used, "free_left": self.free_left,
                "paid": self.paid, "available": self.available, "unlimited": self.unlimited, "guest": self.guest}


class Quota:
    def __init__(self, db: Database, limits: Limits):
        self.db = db
        self.limits = limits

    def balance(self, user_id: str, network: str = "") -> Balance:
        """What the user may still convert. For a guest, `network` is where the request comes from."""
        user = self.db.user(user_id) or {}
        role = user.get("role")
        guest = role == "guest"
        allowance = user.get("monthly_pages")
        if allowance is None:
            allowance = self.limits.guest_pages_per_month if guest else self.limits.free_pages_per_month
        allowance = int(allowance)
        since = month_start()
        free_used = paid = 0
        for row in self.db.usage_rows(user_id):
            sign = {"charge": 1, "refund": -1, "grant": -1, "purchase": -1}[row["kind"]]
            if row["bucket"] == "free":
                if row["created"] >= since:
                    free_used += sign * row["pages"]
            else:
                paid -= sign * row["pages"]
        network_left = None
        if guest:
            pool = self.limits.guest_pages_per_month - self.db.guest_pages_since(network or user.get("last_ip", ""), since)
            network_left = max(pool, 0)
        return Balance(allowance, max(free_used, 0), max(paid, 0), unlimited=role == "admin", guest=guest,
                       network_left=network_left)

    def charge(self, user_id: str, job_id: str, pages: int, network: str = "") -> bool:
        """Take `pages` from the free allowance first, then from paid pages; False (and nothing taken) when
        the user doesn't have enough. Admins always can; their pages are recorded as free."""
        with self.db._lock:
            balance = self.balance(user_id, network)
            if pages > balance.available:
                return False
            from_free = pages if balance.unlimited else min(pages, balance.free_left)
            if from_free:
                self.db.add_usage(user_id, "charge", "free", from_free, job_id, ip=network)
            if pages - from_free:
                self.db.add_usage(user_id, "charge", "paid", pages - from_free, job_id, ip=network)
            return True

    def refund(self, user_id: str, job_id: str, note: str = "conversion failed") -> int:
        """Give back what a job was charged (once)."""
        with self.db._lock:
            rows = [r for r in self.db.usage_rows(user_id) if r["job_id"] == job_id]
            refunded = 0
            for bucket in ("free", "paid"):
                charged = sum(r["pages"] for r in rows if r["bucket"] == bucket and r["kind"] == "charge")
                back = sum(r["pages"] for r in rows if r["bucket"] == bucket and r["kind"] == "refund")
                if charged - back > 0:
                    self.db.add_usage(user_id, "refund", bucket, charged - back, job_id, note)
                    refunded += charged - back
            return refunded

    def grant(self, user_id: str, pages: int, note: str = "") -> None:
        self.db.add_usage(user_id, "grant", "paid", pages, None, note)
