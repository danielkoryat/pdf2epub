"""SQLite storage for the web UI's conversion history (data/pdf2epub.db).

One row per job: what was converted or enriched, its status, the options, the result summary and report, and the
edits made in the review step. Large files (the uploaded book, the prepared draft, the EPUB) stay in the job's
folder on disk; the database only records them.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id        TEXT PRIMARY KEY,
    kind      TEXT NOT NULL DEFAULT 'pdf',  -- pdf: PDF to EPUB; epub: enrich an existing EPUB
    filename  TEXT NOT NULL,
    title     TEXT NOT NULL DEFAULT '',
    authors   TEXT NOT NULL DEFAULT '',
    language  TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL,
    error     TEXT NOT NULL DEFAULT '',
    review    INTEGER NOT NULL DEFAULT 1,
    created   REAL NOT NULL,
    updated   REAL NOT NULL,
    options   TEXT NOT NULL DEFAULT '{}',   -- JSON
    summary   TEXT,                         -- JSON
    report    TEXT,                         -- JSON
    edits     TEXT,                         -- JSON
    log       TEXT NOT NULL DEFAULT '[]'    -- JSON list of recent log lines
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs (created DESC);
"""

_JSON_FIELDS = ("options", "summary", "report", "edits", "log")


class JobDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def save(self, row: dict[str, Any]) -> None:
        """Insert or update a job row (keys as in the jobs table; JSON fields as Python values)."""
        data = dict(row)
        for key in _JSON_FIELDS:
            if key in data:
                data[key] = None if data[key] is None else json.dumps(data[key], ensure_ascii=False)
        data["updated"] = time.time()
        cols = ", ".join(data)
        marks = ", ".join(f":{k}" for k in data)
        updates = ", ".join(f"{k}=excluded.{k}" for k in data if k != "id")
        with self._lock:
            self._conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks}) ON CONFLICT(id) DO UPDATE SET {updates}", data)

    def delete(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._decode(row) if row else None

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jobs ORDER BY created DESC").fetchall()
        return [self._decode(r) for r in rows]

    def ids(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._conn.execute("SELECT id FROM jobs")}

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in _JSON_FIELDS:
            if data.get(key) is not None:
                data[key] = json.loads(data[key])
        data["review"] = bool(data["review"])
        return data
