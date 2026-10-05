#!/usr/bin/env python3
"""Orch — serial-queue job orchestrator (Phase 1).

Single file: core (DB, schedule, jobs, queue), engine, executor and CLI.
Python 3.10+ stdlib only (pyodbc optional for the `sql` job type).
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import logging
import os
import queue as queue_mod
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time

SCHEMA_VERSION = 1
TS_FMT = "%Y-%m-%d %H:%M:%S"
ORCH_FILE = os.path.abspath(__file__)


class OrchError(Exception):
    """User-facing error (bad input, missing job, validation failure...)."""


# ---------------------------------------------------------------------------
# Time helpers — local naive "YYYY-MM-DD HH:MM:SS" strings (no DST handling)
# ---------------------------------------------------------------------------

def now_dt() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


def fmt_ts(d: dt.datetime) -> str:
    return d.strftime(TS_FMT)


def now_str() -> str:
    return fmt_ts(now_dt())


_TS_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")


def parse_ts(s: str) -> dt.datetime:
    """Parse 'YYYY-MM-DD HH:MM[:SS]' into a naive datetime."""
    m = _TS_RE.match(s or "")
    if not m:
        raise OrchError(f"invalid timestamp {s!r} (expected YYYY-MM-DD HH:MM[:SS])")
    y, mo, d, h, mi, sec = (int(x) if x else 0 for x in m.groups())
    try:
        return dt.datetime(y, mo, d, h, mi, sec)
    except ValueError as e:
        raise OrchError(f"invalid timestamp {s!r}: {e}") from None


def add_seconds(ts: str, seconds: float) -> str:
    return fmt_ts(parse_ts(ts) + dt.timedelta(seconds=seconds))


# ---------------------------------------------------------------------------
# Schedules: cron / every / at / startup
# ---------------------------------------------------------------------------

class CronExpr:
    """5-field cron `m h dom mon dow` with *, lists, ranges and steps.

    dow 0-7 (0 and 7 = Sunday). If both dom and dow are restricted they are
    OR-ed (standard cron). next_after() skips whole months/days/hours.
    """

    FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("dom", 1, 31), ("month", 1, 12), ("dow", 0, 7))

    def __init__(self, expr: str):
        self.expr = expr
        parts = (expr or "").split()
        if len(parts) != 5:
            raise OrchError(f"cron {expr!r}: expected 5 fields (m h dom mon dow)")
        sets = []
        for text, (name, lo, hi) in zip(parts, self.FIELDS):
            sets.append(self._parse_field(text, name, lo, hi))
        self.minutes, self.hours, self.doms, self.months, dows = sets
        self.dows = {0 if d == 7 else d for d in dows}
        self.dom_any = parts[2].startswith("*")
        self.dow_any = parts[4].startswith("*")
        self._minutes_sorted = sorted(self.minutes)

    @staticmethod
    def _parse_field(text: str, name: str, lo: int, hi: int) -> set[int]:
        out: set[int] = set()
        for item in text.split(","):
            m = re.fullmatch(r"(\*|\d+)(?:-(\d+))?(?:/(\d+))?", item)
            if not m:
                raise OrchError(f"cron field {name} {text!r}: invalid item {item!r}")
            start_s, end_s, step_s = m.groups()
            step = int(step_s) if step_s else 1
            if step < 1:
                raise OrchError(f"cron field {name} {text!r}: step must be >= 1")
            if start_s == "*":
                if end_s:
                    raise OrchError(f"cron field {name} {text!r}: invalid item {item!r}")
                a, b = lo, hi
            else:
                a = int(start_s)
                # "5/10" means 5..max step 10; "5" alone means just 5
                b = int(end_s) if end_s else (hi if step_s else a)
            if not (lo <= a <= hi and lo <= b <= hi) or a > b:
                raise OrchError(f"cron field {name} {text!r}: out of range {lo}-{hi}")
            out.update(range(a, b + 1, step))
        return out

    def _day_ok(self, d: dt.datetime) -> bool:
        dom_ok = d.day in self.doms
        dow_ok = (d.weekday() + 1) % 7 in self.dows  # python Mon=0 -> cron Sun=0
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow_ok
        if self.dow_any:
            return dom_ok
        return dom_ok or dow_ok

    def next_after(self, after: dt.datetime) -> dt.datetime:
        """First matching minute strictly after `after`."""
        t = after.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        limit = after.year + 5
        while t.year <= limit:
            if t.month not in self.months:
                y, m = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
                t = dt.datetime(y, m, 1)
                continue
            if not self._day_ok(t):
                t = dt.datetime(t.year, t.month, t.day) + dt.timedelta(days=1)
                continue
            if t.hour not in self.hours:
                t = t.replace(minute=0) + dt.timedelta(hours=1)
                continue
            nxt = next((mi for mi in self._minutes_sorted if mi >= t.minute), None)
            if nxt is None:
                t = t.replace(minute=0) + dt.timedelta(hours=1)
                continue
            return t.replace(minute=nxt)
        raise OrchError(f"cron {self.expr!r}: no matching time within 5 years")


_EVERY_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.I)
_EVERY_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_every(expr: str) -> int:
    """'30s' / '5m' / '2h' / '1d' / '90' -> seconds (> 0)."""
    m = _EVERY_RE.match(str(expr or ""))
    if not m:
        raise OrchError(f"every {expr!r}: expected e.g. 30s, 5m, 2h, 1d or seconds")
    sec = int(m.group(1)) * _EVERY_UNITS[m.group(2).lower()]
    if sec <= 0:
        raise OrchError(f"every {expr!r}: interval must be > 0")
    return sec


SCHEDULE_KINDS = ("cron", "every", "at", "startup")


def validate_schedule(kind: str, expr: str | None, now: dt.datetime | None = None) -> str | None:
    """Validate and normalise a schedule; return normalised expr."""
    now = now or now_dt()
    if kind == "cron":
        CronExpr(expr)
        return " ".join(expr.split())
    if kind == "every":
        parse_every(expr)
        return str(expr).strip()
    if kind == "at":
        t = parse_ts(expr)
        if t <= now:
            raise OrchError(f"at {expr!r}: time is in the past")
        return fmt_ts(t)
    if kind == "startup":
        return None
    raise OrchError(f"unknown schedule kind {kind!r} (use {', '.join(SCHEDULE_KINDS)})")


def next_fire(kind: str, expr: str | None, after: dt.datetime) -> dt.datetime | None:
    """Next fire time strictly after `after` (None if the schedule never fires again).

    For `every` this is `after + interval` so callers anchor on the previous fire.
    """
    if kind == "cron":
        return CronExpr(expr).next_after(after)
    if kind == "every":
        return after + dt.timedelta(seconds=parse_every(expr))
    if kind == "at":
        t = parse_ts(expr)
        return t if t > after else None
    return None


def initial_next_run(kind: str, expr: str | None, now: dt.datetime | None = None) -> str | None:
    now = now or now_dt()
    if kind == "at":
        return fmt_ts(parse_ts(expr))
    t = next_fire(kind, expr, now)
    return fmt_ts(t) if t else None


def parse_schedule_line(line: str) -> tuple[str, str | None]:
    """'cron: */5 * * * *' | 'every: 10m' | 'at: 2026-10-01 08:00' | 'startup'."""
    line = line.strip()
    if line.lower() == "startup":
        return "startup", None
    kind, sep, expr = line.partition(":")
    kind = kind.strip().lower()
    if not sep or kind not in SCHEDULE_KINDS:
        raise OrchError(f"schedule {line!r}: expected 'cron: ...', 'every: ...', 'at: ...' or 'startup'")
    return kind, expr.strip()


def format_schedule(kind: str, expr: str | None) -> str:
    return "startup" if kind == "startup" else f"{kind}: {expr}"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS job_def (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    job_type        TEXT NOT NULL,
    params          TEXT NOT NULL DEFAULT '{}',
    queue           TEXT NOT NULL DEFAULT 'default',
    priority        INTEGER NOT NULL DEFAULT 5,
    timeout_sec     INTEGER NOT NULL DEFAULT 3600,
    max_retry       INTEGER NOT NULL DEFAULT 0,
    retry_delay_sec INTEGER NOT NULL DEFAULT 60,
    retry_backoff   REAL NOT NULL DEFAULT 2.0,
    dedup_policy    TEXT NOT NULL DEFAULT 'skip',
    missed_policy   TEXT NOT NULL DEFAULT 'run_once',
    after_job       TEXT NULL,
    after_on        TEXT NOT NULL DEFAULT 'success',
    enabled         INTEGER NOT NULL DEFAULT 1,
    description     TEXT,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS schedule (
    id           INTEGER PRIMARY KEY,
    job_id       INTEGER NOT NULL REFERENCES job_def(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    expr         TEXT,
    next_run_at  TEXT,
    last_fire_at TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_schedule_job ON schedule(job_id);

CREATE TABLE IF NOT EXISTS job_run (
    id               INTEGER PRIMARY KEY,
    job_id           INTEGER,
    job_name         TEXT,
    queue            TEXT NOT NULL,
    status           TEXT NOT NULL,
    priority         INTEGER NOT NULL DEFAULT 5,
    trigger          TEXT,
    attempt          INTEGER NOT NULL DEFAULT 1,
    retry_of         INTEGER,
    scheduled_at     TEXT,
    not_before       TEXT,
    started_at       TEXT,
    finished_at      TEXT,
    heartbeat        TEXT,
    pid              INTEGER,
    exit_code        INTEGER,
    error            TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_job_run_queue
    ON job_run(queue, status, priority, scheduled_at, id);
CREATE INDEX IF NOT EXISTS ix_job_run_job ON job_run(job_id, status);

CREATE TABLE IF NOT EXISTS run_log (
    id      INTEGER PRIMARY KEY,
    run_id  INTEGER NOT NULL,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    message TEXT
);
CREATE INDEX IF NOT EXISTS ix_run_log_run ON run_log(run_id, id);

CREATE TABLE IF NOT EXISTS queue_state (
    name   TEXT PRIMARY KEY,
    paused INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS engine_lock (
    queue     TEXT PRIMARY KEY,
    host      TEXT NOT NULL,
    pid       INTEGER NOT NULL,
    heartbeat TEXT NOT NULL
);
"""


def db_path() -> str:
    """DB location: $ORCH_DB, else orch.db next to orch.py (must be local disk)."""
    return os.path.abspath(os.environ.get("ORCH_DB") or os.path.join(os.path.dirname(ORCH_FILE), "orch.db"))


class _Cursor(sqlite3.Cursor):
    def fetchone(self):
        # A partially-stepped SELECT keeps its read transaction (and WAL snapshot) open, so a
        # long-lived connection would stop seeing other processes' writes. Close = reset.
        row = super().fetchone()
        self.close()
        return row


class _Connection(sqlite3.Connection):
    def cursor(self, factory=_Cursor):  # also used by Connection.execute()
        return super().cursor(factory)


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open a connection (one per thread!) in autocommit mode with WAL.

    Transactions are explicit via `tx(con)`. The schema is created on first use.
    """
    path = path or db_path()
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        try:
            os.makedirs(d)
        except FileExistsError:
            pass
    con = sqlite3.connect(path, timeout=30, isolation_level=None, factory=_Connection)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA foreign_keys=ON")
    if con.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    if con.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        init_db(con)
    return con


@contextlib.contextmanager
def tx(con: sqlite3.Connection):
    """Write transaction: BEGIN IMMEDIATE ... COMMIT (ROLLBACK on error)."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def init_db(con: sqlite3.Connection) -> None:
    with tx(con):
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                con.execute(stmt)
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


# ---------------------------------------------------------------------------
# Job definitions: validation + CRUD
# ---------------------------------------------------------------------------

JOB_TYPES = ("command", "powershell", "python", "sql", "http")
DEDUP_POLICIES = ("allow", "skip", "coalesce")
MISSED_POLICIES = ("skip", "run_once", "run_all")
AFTER_ON = ("success", "failure", "always")

JOB_DEFAULTS = {
    "queue": "default", "priority": 5, "timeout_sec": 3600, "max_retry": 0,
    "retry_delay_sec": 60, "retry_backoff": 2.0, "dedup_policy": "skip",
    "missed_policy": "run_once", "after_job": None, "after_on": "success",
    "enabled": 1, "description": None,
}

PARAM_TEMPLATES = {
    "command": {"command": "echo hello", "cwd": None, "env": {}, "encoding": None},
    "powershell": {"file": "C:\\scripts\\job.ps1", "args": ""},
    "python": {"file": "script.py", "args": ""},
    "sql": {"conn": "env:ORCH_SQL_CONN", "query": "SELECT 1"},
    "http": {"url": "https://example.com/health", "method": "GET", "headers": {}, "expect": "2xx"},
}


def _req_str(params: dict, key: str, job_type: str) -> None:
    v = params.get(key)
    if not isinstance(v, str) or not v.strip():
        raise OrchError(f"{job_type}: params.{key} is required (non-empty string)")


def _opt_type(params: dict, key: str, types, job_type: str, desc: str) -> None:
    if params.get(key) is not None and not isinstance(params[key], types):
        raise OrchError(f"{job_type}: params.{key} must be {desc}")


def parse_expect(expect) -> list[tuple[int, int]]:
    """'200,201' / '2xx' / '200-299' -> list of inclusive (lo, hi) ranges. Default 2xx."""
    if expect is None or str(expect).strip() == "":
        return [(200, 299)]
    ranges = []
    for part in str(expect).split(","):
        part = part.strip().lower()
        if re.fullmatch(r"[1-5]xx", part):
            ranges.append((int(part[0]) * 100, int(part[0]) * 100 + 99))
        elif re.fullmatch(r"\d{3}-\d{3}", part):
            lo, hi = map(int, part.split("-"))
            ranges.append((lo, hi))
        elif re.fullmatch(r"\d{3}", part):
            ranges.append((int(part), int(part)))
        else:
            raise OrchError(f"http: invalid expect {expect!r} (e.g. '200,201', '2xx')")
    return ranges


def validate_params(job_type: str, params) -> dict:
    if job_type not in JOB_TYPES:
        raise OrchError(f"unknown job type {job_type!r} (use {', '.join(JOB_TYPES)})")
    if not isinstance(params, dict):
        raise OrchError("params must be a JSON object")
    params = {k: v for k, v in params.items() if v is not None}
    _opt_type(params, "args", (str, list), job_type, "a string or list")
    if isinstance(params.get("args"), list) and not all(isinstance(a, str) for a in params["args"]):
        raise OrchError(f"{job_type}: params.args list must contain strings")
    _opt_type(params, "cwd", str, job_type, "a string")
    _opt_type(params, "env", dict, job_type, "an object")
    if params.get("encoding") is not None:
        import codecs
        try:
            codecs.lookup(params["encoding"])
        except (LookupError, TypeError):
            raise OrchError(f"{job_type}: unknown encoding {params['encoding']!r}") from None
    if job_type == "command":
        _req_str(params, "command", job_type)
    elif job_type == "powershell":
        if bool(params.get("file")) == bool(params.get("command")):
            raise OrchError("powershell: params needs exactly one of 'file' or 'command'")
        _opt_type(params, "file", str, job_type, "a string")
        _opt_type(params, "command", str, job_type, "a string")
    elif job_type == "python":
        _req_str(params, "file", job_type)
    elif job_type == "sql":
        _req_str(params, "conn", job_type)
        if bool(params.get("query")) == bool(params.get("query_file")):
            raise OrchError("sql: params needs exactly one of 'query' or 'query_file'")
    elif job_type == "http":
        _req_str(params, "url", job_type)
        if not re.match(r"^https?://", params["url"], re.I):
            raise OrchError("http: params.url must start with http:// or https://")
        _opt_type(params, "method", str, job_type, "a string")
        _opt_type(params, "headers", dict, job_type, "an object")
        _opt_type(params, "body", (str, dict, list), job_type, "a string or JSON value")
        _opt_type(params, "timeout", (int, float), job_type, "a number")
        parse_expect(params.get("expect"))
    return params


def _validate_fields(f: dict) -> None:
    def _int(key, lo=None):
        v = f.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or (lo is not None and v < lo):
            raise OrchError(f"{key} must be an integer" + (f" >= {lo}" if lo is not None else ""))
    _int("priority")
    _int("timeout_sec", 0)
    _int("max_retry", 0)
    _int("retry_delay_sec", 0)
    if not isinstance(f.get("retry_backoff"), (int, float)) or f["retry_backoff"] <= 0:
        raise OrchError("retry_backoff must be a number > 0")
    if not isinstance(f.get("queue"), str) or not f["queue"].strip():
        raise OrchError("queue must be a non-empty string")
    for key, allowed in (("dedup_policy", DEDUP_POLICIES), ("missed_policy", MISSED_POLICIES),
                         ("after_on", AFTER_ON)):
        if f.get(key) not in allowed:
            raise OrchError(f"{key} must be one of {', '.join(allowed)}")


def _check_after(con, name: str, after: str | None) -> None:
    """after_job must exist and must not create a cycle (chains are single-parent)."""
    if not after:
        return
    if after == name:
        raise OrchError(f"after_job: job {name!r} cannot depend on itself")
    if con.execute("SELECT 1 FROM job_def WHERE name=?", (after,)).fetchone() is None:
        raise OrchError(f"after_job: job {after!r} does not exist")
    seen, cur = {name}, after
    while cur:
        if cur in seen:
            raise OrchError(f"after_job: dependency cycle {name!r} -> {after!r} -> ... -> {cur!r}")
        seen.add(cur)
        row = con.execute("SELECT after_job FROM job_def WHERE name=?", (cur,)).fetchone()
        cur = row["after_job"] if row else None


def _row_to_job(con, row) -> dict:
    job = dict(row)
    job["params"] = json.loads(job["params"] or "{}")
    job["schedules"] = [dict(s) for s in con.execute(
        "SELECT id, kind, expr, next_run_at, last_fire_at, enabled FROM schedule WHERE job_id=? ORDER BY id",
        (job["id"],))]
    nexts = [s["next_run_at"] for s in job["schedules"] if s["enabled"] and s["next_run_at"]]
    job["next_run_at"] = min(nexts) if nexts else None
    return job


def get_job(con, name: str) -> dict:
    row = con.execute("SELECT * FROM job_def WHERE name=?", (name,)).fetchone()
    if row is None:
        raise OrchError(f"job {name!r} not found")
    return _row_to_job(con, row)


def list_jobs(con, name: str | None = None) -> list[dict]:
    if name:
        return [get_job(con, name)]
    return [_row_to_job(con, r) for r in con.execute("SELECT * FROM job_def ORDER BY name")]


def _prepare_schedules(schedules, now: dt.datetime) -> list[tuple[str, str | None, str | None]]:
    out = []
    for item in schedules or ():
        kind, expr = parse_schedule_line(item) if isinstance(item, str) else item
        expr = validate_schedule(kind, expr, now)
        out.append((kind, expr, initial_next_run(kind, expr, now)))
    return out


def _replace_schedules(con, job_id: int, prepared) -> None:
    con.execute("DELETE FROM schedule WHERE job_id=?", (job_id,))
    for kind, expr, nxt in prepared:
        con.execute("INSERT INTO schedule(job_id, kind, expr, next_run_at, enabled) VALUES(?,?,?,?,1)",
                    (job_id, kind, expr, nxt))


def create_job(con, name: str, job_type: str, params: dict, schedules=(), **fields) -> dict:
    """Create a job + its schedules atomically. `schedules`: [(kind, expr)] or ['cron: ...']."""
    name = (name or "").strip()
    if not name:
        raise OrchError("job name is required")
    unknown = set(fields) - set(JOB_DEFAULTS)
    if unknown:
        raise OrchError(f"unknown job field(s): {', '.join(sorted(unknown))}")
    f = dict(JOB_DEFAULTS, **{k: v for k, v in fields.items() if v is not None})
    f["after_job"] = f["after_job"] or None
    f["description"] = f["description"] or None
    f["enabled"] = 1 if f["enabled"] else 0
    _validate_fields(f)
    params = validate_params(job_type, params)
    now = now_dt()
    prepared = _prepare_schedules(schedules, now)
    with tx(con):
        if con.execute("SELECT 1 FROM job_def WHERE name=?", (name,)).fetchone():
            raise OrchError(f"job {name!r} already exists")
        _check_after(con, name, f["after_job"])
        cols = ["name", "job_type", "params", "created_at", *f]
        vals = [name, job_type, json.dumps(params, ensure_ascii=False), fmt_ts(now), *f.values()]
        cur = con.execute(f"INSERT INTO job_def({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})", vals)
        _replace_schedules(con, cur.lastrowid, prepared)
    return get_job(con, name)


def update_job(con, name: str, *, job_type: str | None = None, params: dict | None = None,
               schedules=None, **fields) -> dict:
    """Change only the given fields. schedules=None keeps them; a list replaces all."""
    if "name" in fields:
        raise OrchError("renaming jobs is not supported (chains reference jobs by name)")
    unknown = set(fields) - set(JOB_DEFAULTS)
    if unknown:
        raise OrchError(f"unknown job field(s): {', '.join(sorted(unknown))}")
    now = now_dt()
    prepared = None if schedules is None else _prepare_schedules(schedules, now)
    with tx(con):
        row = con.execute("SELECT * FROM job_def WHERE name=?", (name,)).fetchone()
        if row is None:
            raise OrchError(f"job {name!r} not found")
        f = {k: row[k] for k in JOB_DEFAULTS}
        f.update({k: v for k, v in fields.items() if v is not None})
        for key in ("after_job", "description"):  # "" clears
            if key in fields and fields[key] == "":
                f[key] = None
        f["enabled"] = 1 if f["enabled"] else 0
        _validate_fields(f)
        new_type = job_type or row["job_type"]
        new_params = validate_params(new_type, params if params is not None else json.loads(row["params"]))
        _check_after(con, name, f["after_job"])
        sets = ", ".join(f"{k}=?" for k in f)
        con.execute(f"UPDATE job_def SET job_type=?, params=?, {sets} WHERE id=?",
                    [new_type, json.dumps(new_params, ensure_ascii=False), *f.values(), row["id"]])
        if prepared is not None:
            _replace_schedules(con, row["id"], prepared)
        elif f["enabled"] and not row["enabled"]:
            _reanchor_schedules(con, row["id"], now)
        if f["queue"] != row["queue"]:  # waiting runs follow the job, or no engine would ever take them
            con.execute("UPDATE job_run SET queue=? WHERE job_id=? AND status='Pending'", (f["queue"], row["id"]))
    return get_job(con, name)


def _reanchor_schedules(con, job_id: int, now: dt.datetime) -> None:
    """Re-enabling a job: time spent disabled is not "missed", so restart recurring schedules from now.
    Caller holds the transaction."""
    for s in con.execute("SELECT * FROM schedule WHERE job_id=? AND enabled=1 AND kind IN ('cron','every')"
                         " AND next_run_at < ?", (job_id, fmt_ts(now))).fetchall():
        con.execute("UPDATE schedule SET next_run_at=? WHERE id=?",
                    (initial_next_run(s["kind"], s["expr"], now), s["id"]))


def set_enabled(con, name: str, enabled: bool) -> dict:
    now = now_dt()
    with tx(con):
        row = con.execute("SELECT id FROM job_def WHERE name=?", (name,)).fetchone()
        if row is None:
            raise OrchError(f"job {name!r} not found")
        con.execute("UPDATE job_def SET enabled=? WHERE id=?", (1 if enabled else 0, row["id"]))
        if enabled:
            _reanchor_schedules(con, row["id"], now)
    return get_job(con, name)


def delete_job(con, name: str) -> None:
    with tx(con):
        row = con.execute("SELECT id FROM job_def WHERE name=?", (name,)).fetchone()
        if row is None:
            raise OrchError(f"job {name!r} not found")
        deps = [r[0] for r in con.execute("SELECT name FROM job_def WHERE after_job=? ORDER BY name", (name,))]
        if deps:
            raise OrchError(f"job {name!r} is referenced by after_job of: {', '.join(deps)} "
                            "(change those jobs first)")
        con.execute("DELETE FROM job_def WHERE id=?", (row["id"],))


# ---------------------------------------------------------------------------
# Runs: enqueue + dedup, claim, pause/resume, cancel, logs
# ---------------------------------------------------------------------------

RUN_STATUSES = ("Pending", "Running", "Succeeded", "Failed", "Dead", "Cancelled", "Skipped")
FINAL_STATUSES = ("Succeeded", "Failed", "Dead", "Cancelled", "Skipped")
DEDUP_BYPASS_TRIGGERS = ("manual", "retry")


def add_log(con, run_id: int, level: str, message: str) -> None:
    con.execute("INSERT INTO run_log(run_id, ts, level, message) VALUES(?,?,?,?)",
                (run_id, now_str(), level, message))


def _enqueue(con, job, trigger: str, scheduled_at: str | None = None, not_before: str | None = None,
             attempt: int = 1, retry_of: int | None = None) -> tuple[int, str]:
    """Insert a run for `job` applying its dedup policy. Caller holds the transaction."""
    now = now_str()
    scheduled_at = scheduled_at or now
    policy = job["dedup_policy"] if trigger not in DEDUP_BYPASS_TRIGGERS else "allow"
    pending = []
    if policy in ("skip", "coalesce"):
        pending = [r[0] for r in con.execute(
            "SELECT id FROM job_run WHERE job_id=? AND status='Pending' ORDER BY id", (job["id"],))]

    def insert(status, error=None):
        cur = con.execute(
            "INSERT INTO job_run(job_id, job_name, queue, status, priority, trigger, attempt, retry_of,"
            " scheduled_at, not_before, finished_at, error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (job["id"], job["name"], job["queue"], status, job["priority"], trigger, attempt, retry_of,
             scheduled_at, not_before, now if status != "Pending" else None, error))
        return cur.lastrowid

    if policy == "skip" and pending:
        rid = insert("Skipped", f"dedup(skip): มีงานรออยู่แล้ว #{pending[0]}")
        return rid, "Skipped"
    rid = insert("Pending")
    if policy == "coalesce" and pending:
        con.executemany("UPDATE job_run SET status='Skipped', finished_at=?, error=? WHERE id=? AND status='Pending'",
                        [(now, f"coalesced by #{rid}", p) for p in pending])
    return rid, "Pending"


def enqueue(con, name: str, trigger: str = "manual", scheduled_at: str | None = None) -> tuple[int, str]:
    """Queue a run of job `name`. Returns (run_id, status) — status may be 'Skipped' by dedup."""
    with tx(con):
        job = con.execute("SELECT * FROM job_def WHERE name=?", (name,)).fetchone()
        if job is None:
            raise OrchError(f"job {name!r} not found")
        return _enqueue(con, job, trigger, scheduled_at)


def is_paused(con, queue: str) -> bool:
    row = con.execute("SELECT paused FROM queue_state WHERE name=?", (queue,)).fetchone()
    return bool(row and row[0])


def set_paused(con, queue: str, paused: bool) -> None:
    with tx(con):
        con.execute("INSERT INTO queue_state(name, paused) VALUES(?,?) "
                    "ON CONFLICT(name) DO UPDATE SET paused=excluded.paused", (queue, 1 if paused else 0))


_CLAIM_SQL = ("SELECT * FROM job_run WHERE queue=? AND status='Pending' AND (not_before IS NULL OR not_before <= ?)"
              " ORDER BY priority DESC, scheduled_at ASC, id ASC LIMIT 1")


def claim_next(con, queue: str):
    """Atomically take the next runnable Pending run of `queue` -> Running. None if idle/paused."""
    now = now_str()
    # cheap read-only pre-check so an idle queue never takes the write lock
    if is_paused(con, queue) or con.execute(_CLAIM_SQL, (queue, now)).fetchone() is None:
        return None
    with tx(con):
        if is_paused(con, queue):
            return None
        row = con.execute(_CLAIM_SQL, (queue, now)).fetchone()
        if row is None:
            return None
        con.execute("UPDATE job_run SET status='Running', started_at=?, heartbeat=?, pid=NULL WHERE id=?",
                    (now, now, row["id"]))
    return con.execute("SELECT * FROM job_run WHERE id=?", (row["id"],)).fetchone()


def cancel_run(con, run_id: int) -> str:
    """Pending -> Cancelled now; Running -> cancel_requested=1 (engine kills it). Returns resulting state."""
    with tx(con):
        row = con.execute("SELECT status FROM job_run WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise OrchError(f"run #{run_id} not found")
        if row["status"] == "Pending":
            con.execute("UPDATE job_run SET status='Cancelled', finished_at=?, error='cancelled by user' WHERE id=?",
                        (now_str(), run_id))
            add_log(con, run_id, "WARN", "cancelled while pending")
            return "Cancelled"
        if row["status"] == "Running":
            con.execute("UPDATE job_run SET cancel_requested=1 WHERE id=?", (run_id,))
            return "cancel requested"
        raise OrchError(f"run #{run_id} already finished ({row['status']})")


def get_run(con, run_id: int) -> dict:
    row = con.execute("SELECT * FROM job_run WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise OrchError(f"run #{run_id} not found")
    return dict(row)


def list_runs(con, queue: str | None = None, status: str | None = None, job: str | None = None,
              limit: int = 50) -> list[dict]:
    where, args = [], []
    for col, val in (("queue", queue), ("status", status), ("job_name", job)):
        if val:
            where.append(f"{col}=?")
            args.append(val)
    sql = "SELECT * FROM job_run" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    return [dict(r) for r in con.execute(sql, (*args, limit))]


def get_logs(con, run_id: int, after_id: int = 0) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT id, ts, level, message FROM run_log WHERE run_id=? AND id>? ORDER BY id", (run_id, after_id))]


# ---------------------------------------------------------------------------
# Executor: build command, spawn child, capture output, monitor, kill tree
# ---------------------------------------------------------------------------

IS_WIN = os.name == "nt"
LOG_MAX_LINES = 5000
LOG_MAX_CHARS = 2000
MONITOR_INTERVAL = 0.25
HEARTBEAT_EVERY = 5.0
CANCEL_CHECK_EVERY = 2.0
POSIX_KILL_GRACE = 3.0


def default_encoding() -> str:
    """Console encoding of shell/powershell output: OEM code page on Windows, else utf-8."""
    if IS_WIN:
        try:
            import ctypes
            return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        except Exception:
            pass
    return "utf-8"


def _split_args(args) -> list[str]:
    if not args:
        return []
    if isinstance(args, list):
        return list(args)
    return shlex.split(args, posix=not IS_WIN)


def _with_args(prefix: list[str], args):
    """argv list, or on Windows a command-line string so a raw `args` string passes untouched."""
    if IS_WIN and isinstance(args, str) and args.strip():
        return subprocess.list2cmdline(prefix) + " " + args
    return prefix + _split_args(args)


def _powershell_exe() -> str:
    return shutil.which("powershell") or shutil.which("pwsh") or "powershell"


def build_command(job_type: str, params: dict) -> dict:
    """-> {cmd, shell, env, cwd, encoding} for Popen. Raises OrchError on bad params."""
    params = validate_params(job_type, params)
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (params.get("env") or {}).items()})
    spec = {"shell": False, "env": env, "cwd": params.get("cwd") or None}
    if job_type == "command":
        spec.update(cmd=params["command"], shell=True, encoding=params.get("encoding") or default_encoding())
    elif job_type == "powershell":
        base = [_powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
        if params.get("file"):
            cmd = _with_args(base + ["-File", params["file"]], params.get("args"))
        else:
            cmd = base + ["-Command", params["command"]]
        spec.update(cmd=cmd, encoding=params.get("encoding") or default_encoding())
    elif job_type == "python":
        env["PYTHONIOENCODING"] = "utf-8"
        spec.update(cmd=_with_args([sys.executable, "-u", params["file"]], params.get("args")),
                    encoding=params.get("encoding") or "utf-8")
    else:  # sql / http run in a child `orch.py _exec <type>`; params travel via env, never argv
        env["PYTHONIOENCODING"] = "utf-8"
        env["ORCH_PARAMS"] = json.dumps(params, ensure_ascii=False)
        spec.update(cmd=[sys.executable, "-u", ORCH_FILE, "_exec", job_type], encoding="utf-8")
    return spec


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if IS_WIN:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # access denied -> exists
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class WinJob:
    """Windows Job Object containing a run's process tree.

    `taskkill /T` follows parent pids, so a grandchild whose parent already exited escapes it;
    every descendant stays in the job, so TerminateJobObject still reaches it. No
    KILL_ON_JOB_CLOSE: a normal exit or an engine crash must not kill leftover processes.
    """

    def __init__(self, proc: subprocess.Popen):
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._k32 = k32
        self.handle = k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        if not k32.AssignProcessToJobObject(self.handle, int(proc._handle)):
            err = ctypes.get_last_error()
            self.close()
            raise OSError(err, "AssignProcessToJobObject failed")

    def terminate(self) -> None:
        if self.handle:
            self._k32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            self._k32.CloseHandle(self.handle)
            self.handle = None


def kill_tree(pid: int, proc: subprocess.Popen | None = None, job: WinJob | None = None) -> None:
    """Kill a child and all its descendants."""
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        if job is not None:  # descendants taskkill could not reach (orphaned from the tree)
            job.terminate()
        if proc is not None:
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    if proc is not None:
        try:
            proc.wait(POSIX_KILL_GRACE)
        except subprocess.TimeoutExpired:
            pass
    else:
        time.sleep(POSIX_KILL_GRACE)
    try:  # again even if the leader already exited: grandchildren may linger in the group
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    if proc is not None:
        proc.wait()


def _reader(stream, encoding: str, out: queue_mod.Queue) -> None:
    """Read child output line by line into `out` as (level, message), applying the caps."""
    lines = 0
    warned_long = warned_many = False
    try:
        for raw in iter(stream.readline, b""):
            if lines >= LOG_MAX_LINES:
                if not warned_many:
                    out.put(("WARN", f"output exceeded {LOG_MAX_LINES} lines; further output discarded"))
                    warned_many = True
                continue  # keep draining so the child never blocks on a full pipe
            text = raw.decode(encoding, errors="replace").rstrip("\r\n")
            if len(text) > LOG_MAX_CHARS:
                text = text[:LOG_MAX_CHARS]
                if not warned_long:
                    out.put(("WARN", f"line longer than {LOG_MAX_CHARS} chars truncated"))
                    warned_long = True
            out.put(("OUT", text))
            lines += 1
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _flush_logs(con, run_id: int, q: queue_mod.Queue) -> None:
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue_mod.Empty:
            break
    if items:
        ts = now_str()
        with tx(con):
            con.executemany("INSERT INTO run_log(run_id, ts, level, message) VALUES(?,?,?,?)",
                            [(run_id, ts, lvl, msg) for lvl, msg in items])


def execute_process(con, run_id: int, spec: dict, timeout_sec: int, stop_event: threading.Event) -> dict:
    """Spawn the child for run `run_id` and supervise it until it ends.

    Returns {"outcome": exited|timeout|cancelled|shutdown|start_failed, "exit_code", "error"}.
    """
    kw = {}
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    try:
        proc = subprocess.Popen(spec["cmd"], shell=spec["shell"], cwd=spec["cwd"], env=spec["env"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
    except (OSError, ValueError) as e:
        return {"outcome": "start_failed", "exit_code": None, "error": f"cannot start process: {e}"}

    job = None
    if IS_WIN:
        try:
            job = WinJob(proc)
        except OSError as e:
            add_log(con, run_id, "WARN", f"job object unavailable ({e}); orphaned descendants may survive a kill")
    try:
        return _supervise(con, run_id, proc, job, spec, timeout_sec, stop_event)
    finally:
        if job is not None:
            job.close()


def _supervise(con, run_id: int, proc: subprocess.Popen, job, spec: dict, timeout_sec: int,
               stop_event: threading.Event) -> dict:
    con.execute("UPDATE job_run SET pid=?, heartbeat=? WHERE id=?", (proc.pid, now_str(), run_id))
    add_log(con, run_id, "INFO", f"started pid {proc.pid}")
    logq: queue_mod.Queue = queue_mod.Queue()
    reader = threading.Thread(target=_reader, args=(proc.stdout, spec["encoding"], logq), daemon=True)
    reader.start()

    start = last_hb = last_cancel = time.monotonic()
    outcome, error = "exited", None
    try:
        while proc.poll() is None:
            stop_event.wait(MONITOR_INTERVAL)
            _flush_logs(con, run_id, logq)
            t = time.monotonic()
            if stop_event.is_set():
                outcome, error = "shutdown", "engine shutting down"
            elif timeout_sec and t - start >= timeout_sec:
                outcome, error = "timeout", f"timeout after {timeout_sec}s"
            elif t - last_cancel >= CANCEL_CHECK_EVERY:
                last_cancel = t
                if con.execute("SELECT cancel_requested FROM job_run WHERE id=?", (run_id,)).fetchone()[0]:
                    outcome, error = "cancelled", "cancelled by user"
            if outcome != "exited":
                add_log(con, run_id, "WARN", f"{error}: killing process tree (pid {proc.pid})")
                kill_tree(proc.pid, proc, job)
                break
            if t - last_hb >= HEARTBEAT_EVERY:
                last_hb = t
                con.execute("UPDATE job_run SET heartbeat=? WHERE id=?", (now_str(), run_id))
    except BaseException:
        # unexpected failure (e.g. DB error): never leave the child running unsupervised
        kill_tree(proc.pid, proc, job)
        raise

    reader.join(5)
    if reader.is_alive():
        logq.put(("WARN", "output pipe still held open by a descendant process; stopped reading"))
    _flush_logs(con, run_id, logq)
    code = proc.wait()
    if outcome == "exited" and code != 0:
        error = f"exit code {code}"
    add_log(con, run_id, "INFO" if outcome == "exited" and code == 0 else "WARN",
            f"process ended: {outcome}, exit code {code}")
    return {"outcome": outcome, "exit_code": code, "error": error}


# ---------------------------------------------------------------------------
# Running a claimed run: finalize, retry/backoff, dependency chain
# ---------------------------------------------------------------------------

def _chain_matches(after_on: str, status: str) -> bool:
    return (after_on == "always" and status in ("Succeeded", "Dead")) or \
        (after_on == "success" and status == "Succeeded") or (after_on == "failure" and status == "Dead")


def finalize(con, run_id: int, outcome: str, exit_code: int | None = None, error: str | None = None) -> str:
    """Close a Running run. Schedules a retry or triggers dependent jobs. Returns final status."""
    with tx(con):
        run = con.execute("SELECT * FROM job_run WHERE id=?", (run_id,)).fetchone()
        job = con.execute("SELECT * FROM job_def WHERE id=?", (run["job_id"],)).fetchone()
        now = now_str()
        if outcome == "exited" and exit_code == 0:
            status = "Succeeded"
        elif outcome == "cancelled":
            status = "Cancelled"
        elif job is not None and run["attempt"] <= job["max_retry"]:
            status = "Failed"
            delay = job["retry_delay_sec"] * job["retry_backoff"] ** (run["attempt"] - 1)
            not_before = add_seconds(now, delay)
            rid, _ = _enqueue(con, job, "retry", scheduled_at=run["scheduled_at"], not_before=not_before,
                              attempt=run["attempt"] + 1, retry_of=run_id)
            add_log(con, run_id, "WARN", f"attempt {run['attempt']} failed; retry #{rid} not before {not_before}")
        else:
            status = "Dead"
        con.execute("UPDATE job_run SET status=?, finished_at=?, exit_code=?, error=?, cancel_requested=0"
                    " WHERE id=?", (status, now, exit_code, error, run_id))
        add_log(con, run_id, "INFO" if status == "Succeeded" else "WARN",
                f"finished: {status}" + (f" ({error})" if error else ""))
        if status in ("Succeeded", "Dead"):
            for dep in con.execute("SELECT * FROM job_def WHERE after_job=? ORDER BY id", (run["job_name"],)).fetchall():
                if _chain_matches(dep["after_on"], status):
                    rid, st = _enqueue(con, dep, f"after:{run['job_name']}#{run_id}")
                    add_log(con, run_id, "INFO", f"triggered {dep['name']!r}: run #{rid} ({st})")
    return status


def requeue(con, run_id: int, reason: str) -> None:
    """Put a Running run back to Pending without counting an attempt (graceful shutdown)."""
    with tx(con):
        con.execute("UPDATE job_run SET status='Pending', started_at=NULL, heartbeat=NULL, pid=NULL,"
                    " exit_code=NULL, error=NULL WHERE id=?", (run_id,))
        add_log(con, run_id, "WARN", f"returned to Pending: {reason}")


def run_job(con, run, stop_event: threading.Event) -> str:
    """Execute a claimed (Running) run to completion. Returns its resulting status."""
    run_id = run["id"]
    job = con.execute("SELECT * FROM job_def WHERE id=?", (run["job_id"],)).fetchone()
    if job is None:
        return finalize(con, run_id, "config_error", None, "definition deleted")
    if not job["enabled"] and run["trigger"] != "manual":
        with tx(con):
            con.execute("UPDATE job_run SET status='Skipped', finished_at=?, error='job disabled' WHERE id=?",
                        (now_str(), run_id))
            add_log(con, run_id, "WARN", "skipped: job disabled")
        return "Skipped"
    if stop_event.is_set():
        requeue(con, run_id, "engine shutting down before start")
        return "Pending"
    add_log(con, run_id, "INFO", f"run #{run_id} of {job['name']!r} attempt {run['attempt']} (trigger {run['trigger']})")
    try:
        spec = build_command(job["job_type"], json.loads(job["params"] or "{}"))
    except (OrchError, ValueError) as e:
        result = {"outcome": "config_error", "exit_code": None, "error": f"config error: {e}"}
    else:
        result = execute_process(con, run_id, spec, job["timeout_sec"], stop_event)
    if result["outcome"] == "shutdown":
        requeue(con, run_id, "engine shut down while running (not counted as an attempt)")
        return "Pending"
    return finalize(con, run_id, result["outcome"], result["exit_code"], result["error"])


MISSED_GRACE_SEC = 90
MISSED_CAP = 1000
PRUNE_DEFAULT_DAYS = 30

log = logging.getLogger("orch")


def _fire_schedule(con, s, job, now: dt.datetime) -> list[int]:
    """Enqueue the due fire times of schedule `s` per missed policy; advance next_run_at. In a tx."""
    fires, t = [], parse_ts(s["next_run_at"])
    while t is not None and t <= now:
        fires.append(t)
        if len(fires) >= MISSED_CAP:
            t = next_fire(s["kind"], s["expr"], now)
            break
        t = next_fire(s["kind"], s["expr"], t)
    on_time = [f for f in fires if (now - f).total_seconds() <= MISSED_GRACE_SEC]
    missed = [f for f in fires if (now - f).total_seconds() > MISSED_GRACE_SEC]
    policy = job["missed_policy"]
    chosen = (missed if policy == "run_all" else missed[-1:] if policy == "run_once" else []) + on_time
    if missed:
        log.warning("job %r %s: %d missed fire(s) since %s, policy %s -> enqueue %d",
                    job["name"], format_schedule(s["kind"], s["expr"]), len(missed), fmt_ts(missed[0]),
                    policy, len(chosen) - len(on_time))
    ids = [_enqueue(con, job, s["kind"], scheduled_at=fmt_ts(f))[0] for f in sorted(chosen)]
    con.execute("UPDATE schedule SET next_run_at=?, last_fire_at=?, enabled=? WHERE id=?",
                (fmt_ts(t) if t else None, fmt_ts(fires[-1]), 0 if s["kind"] == "at" else 1, s["id"]))
    return ids


_DUE_SQL = ("SELECT s.id AS sid FROM schedule s JOIN job_def j ON j.id=s.job_id"
            " WHERE j.queue=? AND j.enabled=1 AND s.enabled=1 AND s.kind!='startup'"
            " AND s.next_run_at IS NOT NULL AND s.next_run_at<=? ORDER BY s.next_run_at, s.id")


def scheduler_tick(con, queue: str, now: dt.datetime | None = None) -> list[int]:
    """Fire every due schedule of jobs in `queue`. Returns created run ids."""
    now = now or now_dt()
    if con.execute(_DUE_SQL, (queue, fmt_ts(now))).fetchone() is None:
        return []
    ids = []
    with tx(con):
        for (sid,) in con.execute(_DUE_SQL, (queue, fmt_ts(now))).fetchall():
            s = con.execute("SELECT * FROM schedule WHERE id=?", (sid,)).fetchone()
            job = con.execute("SELECT * FROM job_def WHERE id=?", (s["job_id"],)).fetchone()
            ids += _fire_schedule(con, s, job, now)
    return ids


def fire_startup(con, queue: str) -> list[int]:
    """Enqueue jobs of `queue` that have a `startup` schedule (called when the engine starts)."""
    ids = []
    with tx(con):
        rows = con.execute("SELECT s.id AS sid, j.* FROM schedule s JOIN job_def j ON j.id=s.job_id"
                           " WHERE j.queue=? AND j.enabled=1 AND s.enabled=1 AND s.kind='startup'"
                           " ORDER BY s.id", (queue,)).fetchall()
        for r in rows:
            ids.append(_enqueue(con, r, "startup")[0])
            con.execute("UPDATE schedule SET last_fire_at=? WHERE id=?", (now_str(), r["sid"]))
    return ids


def prune(con, days: int = PRUNE_DEFAULT_DAYS) -> int:
    """Delete finished runs (and their logs) older than `days`. Returns runs deleted."""
    if days < 0:
        raise OrchError("--days must be >= 0")
    cutoff = fmt_ts(now_dt() - dt.timedelta(days=days))
    final = ",".join(f"'{s}'" for s in FINAL_STATUSES)
    with tx(con):
        where = f"status IN ({final}) AND finished_at IS NOT NULL AND finished_at < ?"
        con.execute(f"DELETE FROM run_log WHERE run_id IN (SELECT id FROM job_run WHERE {where})", (cutoff,))
        return con.execute(f"DELETE FROM job_run WHERE {where}", (cutoff,)).rowcount


class SchedulerThread(threading.Thread):
    """Fires schedules every second (independently of the worker), calls `lock_heartbeat`
    every `lock_every` seconds and prunes hourly. Uses its own connection."""

    TICK = 1.0
    PRUNE_EVERY = 3600.0

    def __init__(self, queue: str, stop_event: threading.Event, lock_heartbeat=None, lock_every: float = 5.0,
                 prune_days: int = PRUNE_DEFAULT_DAYS):
        super().__init__(name=f"scheduler-{queue}", daemon=True)
        self.queue, self.stop_event = queue, stop_event
        self.lock_heartbeat, self.lock_every, self.prune_days = lock_heartbeat, lock_every, prune_days

    def run(self):
        con = connect()
        last_lock = last_prune = time.monotonic()
        try:
            while not self.stop_event.is_set():
                try:
                    ids = scheduler_tick(con, self.queue)
                    if ids:
                        log.info("scheduled run(s) %s", ", ".join(f"#{i}" for i in ids))
                    t = time.monotonic()
                    if self.lock_heartbeat and t - last_lock >= self.lock_every:
                        last_lock = t
                        self.lock_heartbeat(con)
                    if t - last_prune >= self.PRUNE_EVERY:
                        last_prune = t
                        n = prune(con, self.prune_days)
                        if n:
                            log.info("pruned %d old run(s)", n)
                except Exception:
                    log.exception("scheduler tick failed")
                self.stop_event.wait(self.TICK)
        finally:
            con.close()


def process_one(con, queue: str, stop_event: threading.Event) -> bool:
    """Claim and run one run of `queue`. False if nothing was runnable."""
    run = claim_next(con, queue)
    if run is None:
        return False
    log.info("run #%d %s: started (attempt %d, trigger %s)", run["id"], run["job_name"], run["attempt"], run["trigger"])
    try:
        status = run_job(con, run, stop_event)
    except Exception as e:
        log.exception("run #%d %s: engine error", run["id"], run["job_name"])
        try:  # close the run so the queue stays serial; recover() handles it if even this fails
            status = finalize(con, run["id"], "engine_error", None, f"engine error: {e}")
        except Exception:
            log.exception("run #%d: could not finalize after engine error", run["id"])
            raise
    log.info("run #%d %s: %s", run["id"], run["job_name"], status)
    return True


# ---------------------------------------------------------------------------
# Engine: lock, recovery, worker loop + scheduler thread, graceful shutdown
# ---------------------------------------------------------------------------

LOCK_STALE_SEC = 30
EXIT_LOCKED = 3
EXIT_LOCK_LOST = 4


def acquire_lock(con, queue: str, host: str, pid: int) -> None:
    with tx(con):
        row = con.execute("SELECT * FROM engine_lock WHERE queue=?", (queue,)).fetchone()
        fresh_after = fmt_ts(now_dt() - dt.timedelta(seconds=LOCK_STALE_SEC))
        if row and (row["host"], row["pid"]) != (host, pid) and row["heartbeat"] > fresh_after:
            raise OrchError(f"an engine for queue {queue!r} is already running "
                            f"(host {row['host']}, pid {row['pid']}, heartbeat {row['heartbeat']})")
        if row and (row["host"], row["pid"]) != (host, pid):
            log.warning("taking over stale engine lock of queue %r (host %s, pid %s, heartbeat %s)",
                        queue, row["host"], row["pid"], row["heartbeat"])
        con.execute("INSERT OR REPLACE INTO engine_lock(queue, host, pid, heartbeat) VALUES(?,?,?,?)",
                    (queue, host, pid, now_str()))


def heartbeat_lock(con, queue: str, host: str, pid: int) -> bool:
    """Refresh our lock. False if another engine took it over."""
    return con.execute("UPDATE engine_lock SET heartbeat=? WHERE queue=? AND pid=? AND host=?",
                       (now_str(), queue, pid, host)).rowcount > 0


def release_lock(con, queue: str, host: str, pid: int) -> None:
    con.execute("DELETE FROM engine_lock WHERE queue=? AND pid=? AND host=?", (queue, pid, host))


def engine_states(con) -> list[dict]:
    """Per known queue: engine running? (fresh lock heartbeat) and paused?"""
    fresh_after = fmt_ts(now_dt() - dt.timedelta(seconds=LOCK_STALE_SEC))
    names = {r[0] for r in con.execute("SELECT queue FROM job_def UNION SELECT name FROM queue_state"
                                       " UNION SELECT queue FROM engine_lock")}
    out = []
    for q in sorted(names):
        lk = con.execute("SELECT * FROM engine_lock WHERE queue=?", (q,)).fetchone()
        out.append({"queue": q, "running": bool(lk and lk["heartbeat"] > fresh_after),
                    "host": lk["host"] if lk else None, "pid": lk["pid"] if lk else None,
                    "heartbeat": lk["heartbeat"] if lk else None, "paused": is_paused(con, q)})
    return out


def recover(con, queue: str) -> list[int]:
    """Fail runs left Running by a dead engine of this queue (normal retry rules apply)."""
    ids = []
    for run in con.execute("SELECT * FROM job_run WHERE queue=? AND status='Running' ORDER BY id",
                           (queue,)).fetchall():
        msg = f"engine lost while running (pid {run['pid']}, last heartbeat {run['heartbeat']})"
        log.warning("run #%d %s: %s", run["id"], run["job_name"], msg)
        add_log(con, run["id"], "WARN", msg)
        if pid_alive(run["pid"]):
            warn = (f"pid {run['pid']} is still alive: the job may still be running — check it manually "
                    "(not killed automatically because of pid reuse risk)")
            log.warning("run #%d: %s", run["id"], warn)
            add_log(con, run["id"], "WARN", warn)
        finalize(con, run["id"], "engine_lost", run["exit_code"], "engine lost")
        ids.append(run["id"])
    return ids


def setup_engine_logging(queue: str) -> str:
    """Log to stdout (if any) and logs/engine-<queue>.log next to the DB."""
    from logging.handlers import RotatingFileHandler
    log_dir = os.path.join(os.path.dirname(db_path()), "logs")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"engine-{re.sub(r'[^A-Za-z0-9_.-]', '_', queue)}.log")
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    log.handlers.clear()
    log.setLevel(logging.INFO)
    log.propagate = False
    fh = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    return path


def run_engine(queue: str, poll: float = 1.0) -> int:
    log_path = setup_engine_logging(queue)
    import socket
    host, pid = socket.gethostname(), os.getpid()
    con = connect()
    try:
        acquire_lock(con, queue, host, pid)
    except OrchError as e:
        log.error("%s", e)
        return EXIT_LOCKED
    log.info("engine for queue %r started (pid %d, db %s, log %s)", queue, pid, db_path(), log_path)

    stop = threading.Event()
    state = {"lost": False}

    def on_signal(signum, _frame):
        if not stop.is_set():
            log.warning("signal %s received: shutting down", signum)
        stop.set()
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), on_signal)

    def lock_heartbeat(c):
        if not heartbeat_lock(c, queue, host, pid):
            log.error("engine lock of queue %r was taken by another engine: stopping", queue)
            state["lost"] = True
            stop.set()

    sched = SchedulerThread(queue, stop, lock_heartbeat=lock_heartbeat)
    try:
        for rid in recover(con, queue):
            log.warning("recovered run #%d", rid)
        ids = fire_startup(con, queue)
        if ids:
            log.info("startup trigger queued run(s) %s", ", ".join(f"#{i}" for i in ids))
        sched.start()
        while not stop.is_set():
            try:
                ran = process_one(con, queue, stop)
            except Exception:
                log.exception("worker error")
                ran = False
            if not ran:
                stop.wait(poll)
    finally:
        stop.set()
        if sched.is_alive():
            sched.join(10)
        if not state["lost"]:
            release_lock(con, queue, host, pid)
        con.close()
        log.info("engine for queue %r stopped", queue)
    return EXIT_LOCK_LOST if state["lost"] else 0


# ---------------------------------------------------------------------------
# `_exec sql|http` — runs inside the job's child process (params from ORCH_PARAMS)
# ---------------------------------------------------------------------------

EXEC_MAX_ROWS = 200
HTTP_MAX_BODY_CHARS = 4000


def resolve_secret(value: str) -> str:
    """'env:VAR' -> value of environment variable VAR (read at run time)."""
    if value.startswith("env:"):
        var = value[4:].strip()
        if not var or var not in os.environ:
            raise OrchError(f"environment variable {var!r} is not set")
        return os.environ[var]
    return value


def split_sqlite_statements(sql: str) -> list[str]:
    """Split a script on ';' at statement boundaries (quotes/comments aware via sqlite3)."""
    out, buf = [], ""
    for piece in sql.split(";"):
        buf += piece + ";"
        if sqlite3.complete_statement(buf):
            if buf.strip().rstrip(";").strip():
                out.append(buf.strip())
            buf = ""
    rest = buf.rstrip(";").strip()
    if rest:
        out.append(rest)
    return out


def _print_cursor(cur, rowcount_ok: bool = True) -> None:
    if cur.description:
        print("\t".join(str(d[0]) for d in cur.description))
        n = 0
        for row in cur.fetchall():
            n += 1
            if n <= EXEC_MAX_ROWS:
                print("\t".join("NULL" if v is None else str(v) for v in row))
        if n > EXEC_MAX_ROWS:
            print(f"... ({n - EXEC_MAX_ROWS} more rows not shown)")
        print(f"({n} row{'s' if n != 1 else ''})")
    elif rowcount_ok and cur.rowcount is not None and cur.rowcount >= 0:
        print(f"{cur.rowcount} row(s) affected")


def exec_sql(params: dict) -> int:
    if params.get("query_file"):
        with open(params["query_file"], encoding="utf-8-sig") as f:
            query = f.read()
    else:
        query = params["query"]
    if re.search(r"^\s*GO\s*$", query, re.I | re.M):
        print("error: 'GO' batch separators are not supported; split into separate jobs")
        return 2
    conn = resolve_secret(params["conn"])  # never printed: may contain credentials
    if conn.lower().startswith("sqlite:"):
        con = sqlite3.connect(conn[7:], isolation_level=None)
        try:
            con.execute("BEGIN")
            for stmt in split_sqlite_statements(query):
                _print_cursor(con.execute(stmt))
            con.execute("COMMIT")
        except sqlite3.Error as e:
            if con.in_transaction:
                con.execute("ROLLBACK")
            print(f"SQL error (rolled back): {e}")
            return 1
        finally:
            con.close()
        print("committed")
        return 0
    try:
        import pyodbc
    except ImportError:
        print("error: pyodbc is not installed (pip install pyodbc) — required for non-sqlite connections")
        return 2
    try:
        con = pyodbc.connect(conn, autocommit=False)
    except pyodbc.Error as e:
        print(f"connection error: {e}")
        return 1
    try:
        cur = con.cursor()
        cur.execute(query)
        while True:
            _print_cursor(cur)
            if not cur.nextset():
                break
        con.commit()
    except pyodbc.Error as e:
        con.rollback()
        print(f"SQL error (rolled back): {e}")
        return 1
    finally:
        con.close()
    print("committed")
    return 0


def redact_url(url: str) -> str:
    """URL for logs: drop user:password@ and the query string (tokens often live there)."""
    import urllib.parse
    u = urllib.parse.urlsplit(url)
    host = u.netloc.rpartition("@")[2]
    return urllib.parse.urlunsplit((u.scheme, host, u.path, "", "")) + ("?***" if u.query else "")


def exec_http(params: dict) -> int:
    import urllib.error
    import urllib.parse
    import urllib.request
    body = params.get("body")
    headers = {str(k): str(v) for k, v in (params.get("headers") or {}).items()}
    data = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            if not any(k.lower() == "content-type" for k in headers):
                headers["Content-Type"] = "application/json; charset=utf-8"
        else:
            data = str(body).encode("utf-8")
    method = (params.get("method") or ("POST" if data is not None else "GET")).upper()
    expect = params.get("expect")
    url = params["url"]
    u = urllib.parse.urlsplit(url)
    if u.username is not None:  # urllib can't use user:pass@ itself -> Basic auth header
        import base64
        url = urllib.parse.urlunsplit((u.scheme, u.netloc.rpartition("@")[2], u.path, u.query, u.fragment))
        if not any(k.lower() == "authorization" for k in headers):
            cred = f"{urllib.parse.unquote(u.username)}:{urllib.parse.unquote(u.password or '')}"
            headers["Authorization"] = "Basic " + base64.b64encode(cred.encode("utf-8")).decode("ascii")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    print(f"{method} {redact_url(params['url'])}")
    try:
        resp = urllib.request.urlopen(req, timeout=params.get("timeout", 30))
    except urllib.error.HTTPError as e:
        resp = e
    except (urllib.error.URLError, OSError) as e:
        print(f"request failed: {getattr(e, 'reason', e)}")
        return 1
    with resp:
        status = resp.status if hasattr(resp, "status") else resp.code
        print(f"HTTP {status} {resp.reason}")
        raw = resp.read(HTTP_MAX_BODY_CHARS * 4)
        charset = resp.headers.get_content_charset() or "utf-8"
    text = raw.decode(charset, errors="replace")
    if len(text) > HTTP_MAX_BODY_CHARS:
        text = text[:HTTP_MAX_BODY_CHARS] + " ...(truncated)"
    for line in text.splitlines():
        print(line)
    if not any(lo <= status <= hi for lo, hi in parse_expect(expect)):
        print(f"status {status} not in expected {expect or '2xx'}")
        return 1
    return 0


def exec_main(job_type: str) -> int:
    """Entry point of `orch.py _exec <type>` (internal)."""
    try:
        params = validate_params(job_type, json.loads(os.environ.get("ORCH_PARAMS") or "{}"))
        if job_type == "sql":
            return exec_sql(params)
        if job_type == "http":
            return exec_http(params)
        raise OrchError(f"_exec does not support job type {job_type!r}")
    except (OrchError, OSError, ValueError) as e:
        print(f"error: {e}")
        return 2


# ---------------------------------------------------------------------------
# install-startup: register the engine of a queue with Windows Task Scheduler
# ---------------------------------------------------------------------------

def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def engine_executable() -> str:
    """pythonw.exe (no console window) next to the current interpreter when available."""
    exe = sys.executable
    if IS_WIN:
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(w):
            return w
    return exe


def startup_task_name(queue: str) -> str:
    return f"Orch Engine ({queue})"


def build_startup_script(queue: str, trigger: str, user: str | None = None) -> str:
    """PowerShell that registers one Task Scheduler task starting `orch.py engine --queue Q`.

    Uses Register-ScheduledTask (not schtasks) so the 72h ExecutionTimeLimit can be disabled.
    """
    if trigger not in ("boot", "logon"):
        raise OrchError("--trigger must be boot or logon")
    args = subprocess.list2cmdline([ORCH_FILE, "--db", db_path(), "engine", "--queue", queue])
    lines = [
        f"# Orch: start the engine of queue {queue!r} at {trigger}",
        f"$taskName = {_ps_quote(startup_task_name(queue))}",
        f"$action = New-ScheduledTaskAction -Execute {_ps_quote(engine_executable())} "
        f"-Argument {_ps_quote(args)} -WorkingDirectory {_ps_quote(os.path.dirname(ORCH_FILE))}",
        "$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 "
        "-RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries",
    ]
    if trigger == "boot":
        lines.append("$trigger = New-ScheduledTaskTrigger -AtStartup")
        if user:
            lines += [
                f"$cred = Get-Credential -UserName {_ps_quote(user)} -Message 'Password for the Orch engine account'",
                "Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings "
                "-User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Highest -Force",
            ]
        else:
            lines += [
                "$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest",
                "Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings "
                "-Principal $principal -Force",
            ]
    else:
        who = _ps_quote(user) if user else '"$env:USERDOMAIN\\$env:USERNAME"'
        lines += [
            f"$user = {who}",
            "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user",
            "$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited",
            "Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings "
            "-Principal $principal -Force",
        ]
    lines.append("Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State, "
                 "@{n='ExecutionTimeLimit';e={$_.Settings.ExecutionTimeLimit}}")
    return "\n".join(lines) + "\n"


def cmd_install_startup(args) -> int:
    script = build_startup_script(args.queue, args.trigger, args.user)
    if not args.apply:
        print("# Review, then run in an elevated PowerShell — or re-run with --apply")
        print(script)
        if args.trigger == "boot":
            print("# NOTE: boot tasks run without a user session: never use them for UI automation queues.")
        print(f"# Remove later with: Unregister-ScheduledTask -TaskName {_ps_quote(startup_task_name(args.queue))}"
              " -Confirm:$false")
        return 0
    if not IS_WIN:
        raise OrchError("--apply is only supported on Windows")
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8-sig") as f:
        f.write(script)
        path = f.name
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path])
    finally:
        os.unlink(path)
    if r.returncode != 0:
        raise OrchError("Register-ScheduledTask failed (run from an elevated/Administrator prompt?)")
    print(f"registered task {startup_task_name(args.queue)!r}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _setup_stdout() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _print_table(headers: list[str], rows: list[list]) -> None:
    cells = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip())
    print("  ".join("-" * w for w in widths))
    for r in cells:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip())


_PARAM_FLAGS = ("command", "file", "args", "cwd", "conn", "query", "query_file", "url", "method", "body", "expect")
_FIELD_FLAGS = {"queue": "queue", "priority": "priority", "timeout": "timeout_sec", "retry": "max_retry",
                "retry_delay": "retry_delay_sec", "backoff": "retry_backoff", "dedup": "dedup_policy",
                "missed": "missed_policy", "after": "after_job", "after_on": "after_on", "desc": "description"}


def _params_from_args(args, base: dict) -> dict | None:
    """Merge --params JSON and individual flags onto base; None if nothing given."""
    given = False
    p = dict(base)
    if args.params is not None:
        try:
            extra = json.loads(args.params)
        except json.JSONDecodeError as e:
            raise OrchError(f"--params is not valid JSON: {e}") from None
        if not isinstance(extra, dict):
            raise OrchError("--params must be a JSON object")
        p.update(extra)
        given = True
    for k in _PARAM_FLAGS:
        v = getattr(args, k)
        if v is not None:
            p[k] = v
            given = True
    if args.header:
        headers = dict(p.get("headers") or {})
        for h in args.header:
            k, sep, v = h.partition(":")
            if not sep or not k.strip():
                raise OrchError(f"--header {h!r}: expected key:value")
            headers[k.strip()] = v.strip()
        p["headers"] = headers
        given = True
    return p if given else None


def _fields_from_args(args) -> dict:
    f = {dst: getattr(args, src) for src, dst in _FIELD_FLAGS.items() if getattr(args, src) is not None}
    if args.disabled:
        f["enabled"] = 0
    return f


def _schedules_from_args(args) -> list | None:
    s = [("cron", e) for e in args.cron or ()] + [("every", e) for e in args.every or ()] + \
        [("at", e) for e in args.at or ()] + ([("startup", None)] if args.startup else [])
    return s or None


def schedule_summary(job: dict) -> str:
    return "; ".join(format_schedule(s["kind"], s["expr"]) + ("" if s["enabled"] else " (off)")
                     for s in job["schedules"])


def cmd_init(args) -> int:
    con = connect()
    init_db(con)
    print(f"initialized {db_path()}")
    return 0


def cmd_create(args) -> int:
    con = connect()
    params = _params_from_args(args, {}) or {}
    job = create_job(con, args.name, args.type, params, _schedules_from_args(args) or [], **_fields_from_args(args))
    print(f"created job {job['name']!r} (id {job['id']})")
    return 0


def cmd_change(args) -> int:
    con = connect()
    old = get_job(con, args.name)
    type_changed = args.type is not None and args.type != old["job_type"]
    params = _params_from_args(args, {} if type_changed else old["params"])
    if type_changed and params is None:
        raise OrchError(f"--type changed to {args.type!r}: give its params too")
    job = update_job(con, args.name, job_type=args.type, params=params,
                     schedules=_schedules_from_args(args), **_fields_from_args(args))
    print(f"changed job {job['name']!r}")
    return 0


def cmd_query(args) -> int:
    con = connect()
    jobs = list_jobs(con, args.name)
    if args.json:
        _print_json(jobs)
        return 0
    _print_table(["NAME", "TYPE", "QUEUE", "PRIO", "ENABLED", "SCHEDULE", "NEXT RUN", "AFTER"],
                 [[j["name"], j["job_type"], j["queue"], j["priority"], "yes" if j["enabled"] else "no",
                   schedule_summary(j), j["next_run_at"],
                   f"{j['after_job']} ({j['after_on']})" if j["after_job"] else ""] for j in jobs])
    return 0


def cmd_enable(args) -> int:
    set_enabled(connect(), args.name, True)
    print(f"enabled {args.name!r}")
    return 0


def cmd_disable(args) -> int:
    set_enabled(connect(), args.name, False)
    print(f"disabled {args.name!r}")
    return 0


def cmd_delete(args) -> int:
    delete_job(connect(), args.name)
    print(f"deleted {args.name!r}")
    return 0


def cmd_run(args) -> int:
    rid, status = enqueue(connect(), args.name, "manual")
    print(f"queued run #{rid} of {args.name!r}" if status == "Pending" else f"run #{rid}: {status}")
    return 0


def cmd_runs(args) -> int:
    runs = list_runs(connect(), args.queue, args.status, args.job, args.limit)
    if args.json:
        _print_json(runs)
        return 0
    _print_table(["ID", "JOB", "QUEUE", "STATUS", "PRIO", "TRIGGER", "TRY", "SCHEDULED", "STARTED", "FINISHED",
                  "EXIT", "ERROR"],
                 [[r["id"], r["job_name"], r["queue"], r["status"], r["priority"], r["trigger"], r["attempt"],
                   r["scheduled_at"], r["started_at"], r["finished_at"], r["exit_code"],
                   (r["error"] or "")[:60]] for r in runs])
    return 0


def cmd_end(args) -> int:
    print(f"run #{args.run_id}: {cancel_run(connect(), args.run_id)}")
    return 0


def cmd_logs(args) -> int:
    import time
    con = connect()
    get_run(con, args.run_id)
    last = 0
    while True:
        for line in get_logs(con, args.run_id, last):
            last = line["id"]
            print(f"{line['ts']} {line['level']:<5} {line['message']}", flush=True)
        if not args.follow or get_run(con, args.run_id)["status"] in FINAL_STATUSES:
            if args.follow:  # flush lines written just before finishing
                for line in get_logs(con, args.run_id, last):
                    print(f"{line['ts']} {line['level']:<5} {line['message']}")
            return 0
        time.sleep(0.5)


def cmd_engine(args) -> int:
    return run_engine(args.queue, args.poll)


def cmd_prune(args) -> int:
    print(f"pruned {prune(connect(), args.days)} run(s) older than {args.days} day(s)")
    return 0


def cmd_pause(args) -> int:
    set_paused(connect(), args.queue, True)
    print(f"queue {args.queue!r} paused")
    return 0


def cmd_resume(args) -> int:
    set_paused(connect(), args.queue, False)
    print(f"queue {args.queue!r} resumed")
    return 0


def _add_job_flags(sp, creating: bool) -> None:
    sp.add_argument("name", metavar="NAME")
    sp.add_argument("--type", required=creating, choices=JOB_TYPES)
    g = sp.add_argument_group("params")
    g.add_argument("--command", help="command/powershell: command text")
    g.add_argument("--file", help="powershell/python: script file")
    g.add_argument("--args", help="script arguments (use --args=\"-x 1\" if it starts with '-')")
    g.add_argument("--cwd")
    g.add_argument("--conn", help="sql: connection string, env:VAR or sqlite:<path>")
    g.add_argument("--query")
    g.add_argument("--query-file", dest="query_file")
    g.add_argument("--url")
    g.add_argument("--method")
    g.add_argument("--body")
    g.add_argument("--header", action="append", metavar="K:V")
    g.add_argument("--expect", help="http: expected status e.g. 200,201 or 2xx")
    g.add_argument("--params", metavar="JSON", help="params as a JSON object (flags override)")
    g = sp.add_argument_group("options")
    g.add_argument("--queue")
    g.add_argument("--priority", type=int)
    g.add_argument("--timeout", type=int, help="seconds, 0 = unlimited")
    g.add_argument("--retry", type=int, help="max retries")
    g.add_argument("--retry-delay", dest="retry_delay", type=int, help="seconds")
    g.add_argument("--backoff", type=float)
    g.add_argument("--dedup", choices=DEDUP_POLICIES)
    g.add_argument("--missed", choices=MISSED_POLICIES)
    g.add_argument("--after", metavar="JOB", help='run after JOB ("" clears)')
    g.add_argument("--after-on", dest="after_on", choices=AFTER_ON)
    g.add_argument("--desc")
    g.add_argument("--disabled", action="store_true")
    g = sp.add_argument_group("schedule (repeatable; on change any of these replaces all schedules)")
    g.add_argument("--cron", action="append", metavar="EXPR")
    g.add_argument("--every", action="append", metavar="INTERVAL")
    g.add_argument("--at", action="append", metavar="'YYYY-MM-DD HH:MM'")
    g.add_argument("--startup", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch", description="Serial-queue job orchestrator")
    p.add_argument("--db", metavar="PATH", help="database file (default: $ORCH_DB or orch.db next to orch.py)")
    sub = p.add_subparsers(dest="verb", metavar="VERB")
    sub.required = True

    sp = sub.add_parser("init", help="create DB/schema")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("create", help="create a job")
    _add_job_flags(sp, creating=True)
    sp.set_defaults(func=cmd_create)

    sp = sub.add_parser("change", help="change given fields of a job")
    _add_job_flags(sp, creating=False)
    sp.set_defaults(func=cmd_change)

    sp = sub.add_parser("query", help="list jobs")
    sp.add_argument("name", nargs="?", metavar="NAME")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_query)

    for verb, fn, hlp in (("enable", cmd_enable, "enable a job"), ("disable", cmd_disable, "disable a job"),
                          ("delete", cmd_delete, "delete a job (run history is kept)")):
        sp = sub.add_parser(verb, help=hlp)
        sp.add_argument("name", metavar="NAME")
        sp.set_defaults(func=fn)

    sp = sub.add_parser("run", help="run a job now (manual, bypasses dedup)")
    sp.add_argument("name", metavar="NAME")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("runs", help="run history / queue")
    sp.add_argument("--queue")
    sp.add_argument("--status", choices=RUN_STATUSES)
    sp.add_argument("--job", metavar="NAME")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_runs)

    sp = sub.add_parser("end", help="cancel a run")
    sp.add_argument("run_id", type=int, metavar="RUN_ID")
    sp.set_defaults(func=cmd_end)

    sp = sub.add_parser("logs", help="show a run's log")
    sp.add_argument("run_id", type=int, metavar="RUN_ID")
    sp.add_argument("--follow", "-f", action="store_true")
    sp.set_defaults(func=cmd_logs)

    for verb, fn, hlp in (("pause", cmd_pause, "stop taking new runs from a queue"),
                          ("resume", cmd_resume, "resume a paused queue")):
        sp = sub.add_parser(verb, help=hlp)
        sp.add_argument("--queue", default="default")
        sp.set_defaults(func=fn)

    sp = sub.add_parser("prune", help="delete finished runs + logs older than N days")
    sp.add_argument("--days", type=int, default=PRUNE_DEFAULT_DAYS)
    sp.set_defaults(func=cmd_prune)

    sp = sub.add_parser("engine", help="run the engine of one queue (one job at a time)")
    sp.add_argument("--queue", default="default")
    sp.add_argument("--poll", type=float, default=1.0, help="idle poll interval seconds")
    sp.set_defaults(func=cmd_engine)

    sp = sub.add_parser("install-startup", help="register the engine with Windows Task Scheduler (prints by default)")
    sp.add_argument("--queue", default="default")
    sp.add_argument("--trigger", required=True, choices=("boot", "logon"))
    sp.add_argument("--user", help="boot: run as this account (asks password); logon: whose logon")
    sp.add_argument("--apply", action="store_true", help="actually register (needs Administrator)")
    sp.set_defaults(func=cmd_install_startup)
    return p


def main(argv: list[str] | None = None) -> int:
    _setup_stdout()
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_exec"]:  # internal verb, deliberately not in --help
        if len(argv) != 2:
            print("usage: orch.py _exec sql|http", file=sys.stderr)
            return 2
        return exec_main(argv[1])
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.db:
        os.environ["ORCH_DB"] = os.path.abspath(args.db)
    try:
        return args.func(args) or 0
    except OrchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
