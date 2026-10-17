"""SQLite storage for the web app (data/pdf2epub.db, WAL mode, shared by the web and worker processes).

Tables:
  jobs      one row per conversion/enrichment: owner, status, the pending action for a worker, progress,
            options, summary, report, the user's edits, recent log lines. Files live in data/jobs/<id>/.
  users     people who signed in with Google: role (user/admin), status (active/blocked), last sign-in.
  sessions  sign-in sessions. Only a SHA-256 of the session token is stored, so a copy of the database
            cannot be used to sign in.
  usage     the page ledger: charges, refunds, grants and purchases, from the free monthly allowance or
            from paid pages.
  audit     an append-only record of security-relevant events. Triggers refuse UPDATE and DELETE, and each
            row carries a hash chained to the previous row, so tampering shows.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_V1 = """
CREATE TABLE IF NOT EXISTS jobs (
    id        TEXT PRIMARY KEY,
    kind      TEXT NOT NULL DEFAULT 'pdf',
    filename  TEXT NOT NULL,
    title     TEXT NOT NULL DEFAULT '',
    authors   TEXT NOT NULL DEFAULT '',
    language  TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL,
    error     TEXT NOT NULL DEFAULT '',
    review    INTEGER NOT NULL DEFAULT 1,
    created   REAL NOT NULL,
    updated   REAL NOT NULL,
    options   TEXT NOT NULL DEFAULT '{}',
    summary   TEXT,
    report    TEXT,
    edits     TEXT,
    log       TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs (created DESC);
"""

_V2 = """
ALTER TABLE jobs ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE jobs ADD COLUMN pending TEXT;            -- 'prepare' or 'build' while a worker should act
ALTER TABLE jobs ADD COLUMN stage TEXT NOT NULL DEFAULT '';
ALTER TABLE jobs ADD COLUMN done INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN worker TEXT NOT NULL DEFAULT '';
ALTER TABLE jobs ADD COLUMN claimed_at REAL;
ALTER TABLE jobs ADD COLUMN heartbeat REAL;
ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN pages INTEGER NOT NULL DEFAULT 0;   -- pages of the PDF (charged)
ALTER TABLE jobs ADD COLUMN size INTEGER NOT NULL DEFAULT 0;    -- bytes on disk (input + results)
CREATE INDEX IF NOT EXISTS jobs_owner ON jobs (owner_id, created DESC);
CREATE INDEX IF NOT EXISTS jobs_pending ON jobs (pending, created);

CREATE TABLE IF NOT EXISTS users (
    id           TEXT PRIMARY KEY,
    google_sub   TEXT UNIQUE,
    email        TEXT NOT NULL DEFAULT '',
    name         TEXT NOT NULL DEFAULT '',
    picture      TEXT NOT NULL DEFAULT '',
    role         TEXT NOT NULL DEFAULT 'user',     -- user | admin
    status       TEXT NOT NULL DEFAULT 'active',   -- active | blocked
    monthly_pages INTEGER,                          -- per-user free allowance, NULL = the default
    created      REAL NOT NULL,
    last_login   REAL,
    last_ip      TEXT NOT NULL DEFAULT '',
    last_country TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS users_email ON users (email);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id),
    created    REAL NOT NULL,
    last_seen  REAL NOT NULL,
    expires    REAL NOT NULL,
    ip         TEXT NOT NULL DEFAULT '',
    country    TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id, created DESC);

CREATE TABLE IF NOT EXISTS usage (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  TEXT NOT NULL,
    job_id   TEXT,
    kind     TEXT NOT NULL,     -- charge | refund | grant | purchase
    bucket   TEXT NOT NULL,     -- free | paid
    pages    INTEGER NOT NULL,  -- positive; the kind says which way it goes
    created  REAL NOT NULL,
    note     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS usage_user ON usage (user_id, created);

CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    user_id    TEXT NOT NULL DEFAULT '',
    email      TEXT NOT NULL DEFAULT '',
    ip         TEXT NOT NULL DEFAULT '',
    country    TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    action     TEXT NOT NULL,
    target     TEXT NOT NULL DEFAULT '',
    outcome    TEXT NOT NULL DEFAULT 'ok',   -- ok | denied | error
    details    TEXT NOT NULL DEFAULT '{}',
    prev_hash  TEXT NOT NULL DEFAULT '',
    hash       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit (ts DESC);
CREATE INDEX IF NOT EXISTS audit_user ON audit (user_id, ts DESC);
CREATE INDEX IF NOT EXISTS audit_action ON audit (action, ts DESC);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
"""

# Guests (users without a Google account, role 'guest') are counted by network: the job and the page ledger
# remember the network the upload came from.
_V3 = """
ALTER TABLE jobs ADD COLUMN ip TEXT NOT NULL DEFAULT '';
ALTER TABLE usage ADD COLUMN ip TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS jobs_ip ON jobs (ip, created);
CREATE INDEX IF NOT EXISTS usage_ip ON usage (ip, created);
CREATE INDEX IF NOT EXISTS users_guest_ip ON users (role, last_ip, created);
"""

MIGRATIONS = [_V1, _V2, _V3]

_JSON_FIELDS = ("options", "summary", "report", "edits", "log", "details")
_AUDIT_FIELDS = ("ts", "user_id", "email", "ip", "country", "user_agent", "action", "target", "outcome", "details", "prev_hash")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # -- plumbing ------------------------------------------------------------------------------------------

    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and self._conn.execute("SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone():
                version = 1  # created by the first release, before versioning
            for n, script in enumerate(MIGRATIONS[version:], start=version + 1):
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _statements(script):
                        self._conn.execute(statement)
                    self._conn.execute(f"PRAGMA user_version={n}")
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [self._decode(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in _JSON_FIELDS:
            if isinstance(data.get(key), str) and data[key][:1] in ("{", "["):
                data[key] = json.loads(data[key])
        if "review" in data:
            data["review"] = bool(data["review"])
        return data

    @staticmethod
    def _encode(row: dict[str, Any]) -> dict[str, Any]:
        data = dict(row)
        for key in _JSON_FIELDS:
            if key in data and not isinstance(data[key], str):
                data[key] = None if data[key] is None else json.dumps(data[key], ensure_ascii=False)
        return data

    # -- jobs ----------------------------------------------------------------------------------------------

    def save_job(self, row: dict[str, Any]) -> None:
        data = self._encode(row)
        data["updated"] = time.time()
        cols = ", ".join(data)
        marks = ", ".join(f":{k}" for k in data)
        updates = ", ".join(f"{k}=excluded.{k}" for k in data if k != "id")
        self.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks}) ON CONFLICT(id) DO UPDATE SET {updates}", data)

    def update_job(self, job_id: str, **fields: Any) -> None:
        data = self._encode(fields)
        data["updated"] = time.time()
        sets = ", ".join(f"{k}=:{k}" for k in data)
        self.execute(f"UPDATE jobs SET {sets} WHERE id=:_id", {**data, "_id": job_id})

    def job(self, job_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def jobs(self, owner_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        if owner_id is None:
            return self.query("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,))
        return self.query("SELECT * FROM jobs WHERE owner_id=? ORDER BY created DESC LIMIT ?", (owner_id, limit))

    def delete_job(self, job_id: str) -> None:
        self.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def job_ids(self) -> set[str]:
        return {r["id"] for r in self.query("SELECT id FROM jobs")}

    def claim_job(self, worker: str, kinds: tuple[str, ...] = ("pdf", "epub")) -> dict[str, Any] | None:
        """Atomically hand the next pending job to a worker. Users without a job in progress go first, so one
        person's queue cannot hold everybody else up; within that, oldest first."""
        marks = ",".join("?" for _ in kinds)
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                f"""UPDATE jobs SET status=CASE pending WHEN 'prepare' THEN 'preparing' ELSE 'building' END,
                        worker=?, claimed_at=?, heartbeat=?, attempts=attempts+1, updated=?
                    WHERE id=(SELECT j.id FROM jobs j WHERE j.pending IS NOT NULL AND j.status='queued' AND j.kind IN ({marks})
                              ORDER BY EXISTS(SELECT 1 FROM jobs r WHERE r.owner_id=j.owner_id AND r.status IN ('preparing','building')),
                                       j.updated LIMIT 1)
                    RETURNING *""",
                (worker, now, now, now, *kinds),
            ).fetchone()
        return self._decode(row) if row else None

    def requeue_stale(self, older_than: float, max_attempts: int = 2) -> list[str]:
        """Jobs whose worker stopped sending heartbeats: queue them again, or fail them after max_attempts."""
        cutoff = time.time() - older_than
        stale = self.query("SELECT id, attempts FROM jobs WHERE status IN ('preparing','building') AND heartbeat < ?", (cutoff,))
        for row in stale:
            if row["attempts"] >= max_attempts:
                self.update_job(row["id"], status="failed", pending=None, error="The conversion stopped unexpectedly. Please try again.")
            else:
                self.update_job(row["id"], status="queued", worker="")
        return [r["id"] for r in stale]

    def queue_position(self, job_id: str) -> int:
        row = self.one("SELECT updated FROM jobs WHERE id=?", (job_id,))
        if row is None:
            return 0
        return self.one("SELECT COUNT(*) AS n FROM jobs WHERE status='queued' AND pending IS NOT NULL AND updated < ?", (row["updated"],))["n"]

    # -- users and sessions --------------------------------------------------------------------------------

    def user(self, user_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM users WHERE id=?", (user_id,))

    def upsert_google_user(self, sub: str, email: str, name: str, picture: str, admin: bool, ip: str, country: str) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            existing = self.one("SELECT * FROM users WHERE google_sub=?", (sub,))
            if existing is None:
                user_id = uuid.uuid4().hex
                self.execute(
                    "INSERT INTO users (id, google_sub, email, name, picture, role, created, last_login, last_ip, last_country)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (user_id, sub, email, name, picture, "admin" if admin else "user", now, now, ip, country),
                )
            else:
                user_id = existing["id"]
                role = "admin" if admin else existing["role"]
                self.execute(
                    "UPDATE users SET email=?, name=?, picture=?, role=?, last_login=?, last_ip=?, last_country=? WHERE id=?",
                    (email, name, picture, role, now, ip, country, user_id),
                )
        return self.user(user_id)

    def create_guest(self, network: str, country: str) -> dict[str, Any]:
        user_id = uuid.uuid4().hex
        now = time.time()
        self.execute(
            "INSERT INTO users (id, google_sub, name, role, created, last_login, last_ip, last_country) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, None, "Guest", "guest", now, now, network, country),
        )
        return self.user(user_id)

    def guests_created_since(self, network: str, since: float) -> int:
        return self.one("SELECT COUNT(*) AS n FROM users WHERE role='guest' AND last_ip=? AND created>=?", (network, since))["n"]

    def move_books(self, from_user: str, to_user: str) -> int:
        """Hand a guest's books to the account they signed in with."""
        return self.execute("UPDATE jobs SET owner_id=? WHERE owner_id=?", (to_user, from_user)).rowcount

    def set_user(self, user_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k}=:{k}" for k in fields)
        self.execute(f"UPDATE users SET {sets} WHERE id=:_id", {**fields, "_id": user_id})

    def create_session(self, token: str, user_id: str, days: int, ip: str, country: str, user_agent: str) -> None:
        now = time.time()
        self.execute(
            "INSERT INTO sessions (token_hash, user_id, created, last_seen, expires, ip, country, user_agent) VALUES (?,?,?,?,?,?,?,?)",
            (token_hash(token), user_id, now, now, now + days * 86400, ip, country, user_agent[:300]),
        )

    def session(self, token: str) -> dict[str, Any] | None:
        return self.one(
            "SELECT s.*, u.email, u.name, u.picture, u.role, u.status FROM sessions s JOIN users u ON u.id=s.user_id"
            " WHERE s.token_hash=? AND s.revoked=0 AND s.expires>?",
            (token_hash(token), time.time()),
        )

    def touch_session(self, token: str) -> None:
        self.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (time.time(), token_hash(token)))

    def revoke_session(self, token: str) -> None:
        self.execute("UPDATE sessions SET revoked=1 WHERE token_hash=?", (token_hash(token),))

    def revoke_user_sessions(self, user_id: str) -> None:
        self.execute("UPDATE sessions SET revoked=1 WHERE user_id=?", (user_id,))

    # -- page ledger ---------------------------------------------------------------------------------------

    def add_usage(self, user_id: str, kind: str, bucket: str, pages: int, job_id: str | None = None, note: str = "", ip: str = "") -> None:
        self.execute(
            "INSERT INTO usage (user_id, job_id, kind, bucket, pages, created, note, ip) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, job_id, kind, bucket, pages, time.time(), note, ip),
        )

    def guest_pages_since(self, network: str, since: float) -> int:
        """Pages all guests from one network converted since `since` (charges minus refunds)."""
        row = self.one(
            "SELECT COALESCE(SUM(CASE g.kind WHEN 'charge' THEN g.pages WHEN 'refund' THEN -g.pages ELSE 0 END), 0) AS n"
            " FROM usage g JOIN users u ON u.id=g.user_id WHERE u.role='guest' AND g.job_id IN"
            " (SELECT job_id FROM usage WHERE ip=? AND kind='charge' AND created>=?)",
            (network, since),
        )
        return max(int(row["n"]), 0)

    def usage_rows(self, user_id: str, since: float = 0.0) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM usage WHERE user_id=? AND created>=? ORDER BY id", (user_id, since))

    # -- audit ---------------------------------------------------------------------------------------------

    def audit(self, action: str, outcome: str = "ok", **fields: Any) -> None:
        entry = {
            "ts": time.time(), "user_id": "", "email": "", "ip": "", "country": "", "user_agent": "",
            "action": action, "target": "", "outcome": outcome, "details": {},
        }
        entry.update({k: v for k, v in fields.items() if k in entry})
        entry["user_agent"] = str(entry["user_agent"])[:300]
        entry["details"] = json.dumps(entry["details"], ensure_ascii=False, sort_keys=True)
        with self._lock:
            last = self._conn.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            entry["prev_hash"] = last[0] if last else ""
            entry["hash"] = audit_hash(entry)
            cols = ", ".join(entry)
            self._conn.execute(f"INSERT INTO audit ({cols}) VALUES ({', '.join(':' + k for k in entry)})", entry)

    def audit_rows(self, action: str = "", user_id: str = "", outcome: str = "", limit: int = 200, before_id: int | None = None) -> list[dict[str, Any]]:
        where, params = [], []
        for col, value in (("action", action), ("user_id", user_id), ("outcome", outcome)):
            if value:
                where.append(f"{col}=?" if col != "action" else "action LIKE ?")
                params.append(value if col != "action" else f"{value}%")
        if before_id:
            where.append("id<?")
            params.append(before_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return self.query(f"SELECT * FROM audit {clause} ORDER BY id DESC LIMIT ?", (*params, limit))

    def verify_audit_chain(self) -> tuple[bool, int]:
        """(intact, number of rows checked): recompute the hash chain of the whole audit log."""
        previous = ""
        rows = self.query("SELECT * FROM audit ORDER BY id")
        for row in rows:
            row["details"] = json.dumps(row["details"], ensure_ascii=False, sort_keys=True) if not isinstance(row["details"], str) else row["details"]
            if row["prev_hash"] != previous or audit_hash(row) != row["hash"]:
                return False, len(rows)
            previous = row["hash"]
        return True, len(rows)


def audit_hash(entry: dict[str, Any]) -> str:
    material = json.dumps({k: entry[k] for k in _AUDIT_FIELDS}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _statements(script: str) -> list[str]:
    """Split a migration script into statements; trigger bodies (BEGIN ... END;) stay whole."""
    out, current = [], []
    for line in script.splitlines():
        code = line.split("--", 1)[0].rstrip()
        if not code.strip():
            continue
        current.append(code)
        joined = " ".join(current)
        if code.endswith(";") and (" BEGIN " not in f" {joined} " or joined.rstrip(";").endswith("END")):
            out.append(joined)
            current = []
    if current:
        out.append(" ".join(current))
    return out
