"""Page allowances: every user may convert a number of PDF pages for free each calendar month; beyond that they
use paid pages (bought, or granted by an admin). Pages are charged when a conversion starts and refunded if it
fails. EPUB enrichment and rebuilding a book after editing chapters are free.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import Database


def month_start(now: float | None = None) -> float:
    t = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
    return datetime(t.year, t.month, 1, tzinfo=timezone.utc).timestamp()


@dataclass(frozen=True)
class Balance:
    free_allowance: int
    free_used: int
    paid: int

    @property
    def free_left(self) -> int:
        return max(self.free_allowance - self.free_used, 0)

    @property
    def available(self) -> int:
        return self.free_left + self.paid

    def as_dict(self) -> dict[str, int]:
        return {"free_allowance": self.free_allowance, "free_used": self.free_used, "free_left": self.free_left,
                "paid": self.paid, "available": self.available}


class Quota:
    def __init__(self, db: Database, default_free_pages: int):
        self.db = db
        self.default_free_pages = default_free_pages

    def balance(self, user_id: str) -> Balance:
        user = self.db.user(user_id) or {}
        allowance = user.get("monthly_pages")
        allowance = self.default_free_pages if allowance is None else int(allowance)
        since = month_start()
        free_used = paid = 0
        for row in self.db.usage_rows(user_id):
            sign = {"charge": 1, "refund": -1, "grant": -1, "purchase": -1}[row["kind"]]
            if row["bucket"] == "free":
                if row["created"] >= since:
                    free_used += sign * row["pages"]
            else:
                paid -= sign * row["pages"]
        return Balance(allowance, max(free_used, 0), max(paid, 0))

    def charge(self, user_id: str, job_id: str, pages: int) -> bool:
        """Take `pages` from the free allowance first, then from paid pages; False (and nothing taken) when
        the user doesn't have enough."""
        with self.db._lock:
            balance = self.balance(user_id)
            if pages > balance.available:
                return False
            from_free = min(pages, balance.free_left)
            if from_free:
                self.db.add_usage(user_id, "charge", "free", from_free, job_id)
            if pages - from_free:
                self.db.add_usage(user_id, "charge", "paid", pages - from_free, job_id)
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
