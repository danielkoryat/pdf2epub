"""A consistent snapshot of the database (users, sessions, books, the page ledger, the audit log).

SQLite's online backup copies the database while the app keeps running. The copy is checked, compressed and
kept in <data>/backups; the oldest are removed so that `--keep` remain. The books' files are not included:
they live in <data>/jobs and are covered by backing up the whole volume (see docs/deploy.md).

    pdf2epub-backup [--keep 14]
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from .settings import Settings


def backup(data_dir: Path, keep: int) -> Path:
    source = data_dir / "pdf2epub.db"
    if not source.is_file():
        raise SystemExit(f"no database at {source}")
    folder = data_dir / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")  # sorts by time, unique
    copy = folder / f"pdf2epub-{stamp}.db"
    src, dst = sqlite3.connect(source), sqlite3.connect(copy)
    try:
        src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        src.close()
        dst.close()
    if ok != "ok":
        copy.unlink(missing_ok=True)
        raise SystemExit(f"the backup failed its integrity check: {ok}")
    target = copy.with_suffix(".db.gz")
    with copy.open("rb") as fin, gzip.open(target, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    copy.unlink()
    for old in sorted(folder.glob("pdf2epub-*.db.gz"))[:-keep]:
        old.unlink()
    return target


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pdf2epub-backup", description="Back up the pdf2epub database.")
    p.add_argument("--keep", type=int, default=14, help="how many backups to keep (default: 14)")
    args = p.parse_args(argv)
    settings = Settings.from_env(web=False)
    target = backup(settings.data_dir, max(1, args.keep))
    print(f"backed up to {target} ({target.stat().st_size / 1048576:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
