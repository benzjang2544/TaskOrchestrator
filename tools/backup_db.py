"""Back up orch.db while engines keep running, and keep the newest N copies.

Run it as an Orch job on the default queue, e.g.
    orch create backup-db --type python --file C:\\Orch\\tools\\backup_db.py --cron "0 1 * * *" --retry 1

DB: --db PATH, else $ORCH_DB (the engine passes its own to jobs), else orch.db next to orch.py.
Copies go to <DB folder>\\backup\\orch-YYYYMMDD.db; a second run on the same day replaces that day's copy.
"""
import argparse
import datetime
import glob
import os
import sqlite3
import sys

PATTERN = "orch-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9].db"


def default_db() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.environ.get("ORCH_DB") or os.path.join(root, "orch.db")


def backup(db: str, dest_dir: str, keep: int, today: datetime.date = None) -> str:
    """Copy db with the SQLite online backup API (safe under WAL); return the new file's path."""
    if not os.path.isfile(db):
        raise FileNotFoundError(f"database not found: {db}")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"orch-{(today or datetime.date.today()):%Y%m%d}.db")
    tmp = dest + ".tmp"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(tmp)
        try:
            src.backup(out)
            ok = out.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            out.close()
    finally:
        src.close()
    if ok != "ok":
        os.remove(tmp)
        raise RuntimeError(f"integrity_check of the copy failed: {ok}")
    os.replace(tmp, dest)
    for old in sorted(glob.glob(os.path.join(dest_dir, PATTERN)))[:-keep]:
        os.remove(old)
        print(f"removed old backup {old}")
    return dest


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default=None, help="database file (default: $ORCH_DB or orch.db next to orch.py)")
    p.add_argument("--dest", default=None, help="backup folder (default: backup next to the DB)")
    p.add_argument("--keep", type=int, default=14, help="number of daily copies to keep (default 14)")
    a = p.parse_args(argv)
    if a.keep < 1:
        p.error("--keep must be at least 1")
    db = os.path.abspath(a.db or default_db())
    dest = backup(db, a.dest or os.path.join(os.path.dirname(db), "backup"), a.keep)
    print(f"backed up {db} -> {dest} ({os.path.getsize(dest)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
