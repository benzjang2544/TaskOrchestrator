# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Orch — a Windows Task Scheduler replacement with serial per-queue job execution. `SPEC.md` (Thai) is the
authoritative spec: data model (§3), exact behaviours (§4), CLI (§5), GUI (§6), Windows install (§7),
Build Order (§8), Test Plan (§9). Decisions in SPEC §2 (stdlib only, SQLite WAL on local disk, file layout,
one engine per queue, every job runs as a child process, etc.) must not change without asking the user.
Work proceeds step by step through the §8 Build Order; each step needs passing tests before the next.
SPEC §11 lists risks that must be reported to the user immediately if encountered.

## Commands

```sh
python orch.py init                                   # create DB (default ./orch.db; --db PATH or ORCH_DB)
python orch.py engine --queue default                 # run one queue's engine
python orch.py stop-engine --queue default            # graceful stop via the DB (works for pythonw/boot engines)
python orch_gui.py                                    # GUI
python orch.py install-startup --queue default --trigger boot   # prints the Register-ScheduledTask script (--apply runs it)
setup.bat [-InstallDir C:\Orch] [-Queue Q -Trigger boot|logon]   # one-click install/update (UAC); uninstall.bat keeps data
package.bat                                           # dist\Orch-<date>.zip of the committed files (git archive)
python -m unittest discover -s tests -p "test_*.py"   # all tests (run from repo root)
cd tests && python -m unittest test_06_finalize       # one module
cd tests && python -m unittest test_08_engine.TestEngine.test_graceful_shutdown_requeues   # one test
```

`start_orch.bat` / `orch_engine.bat` / `orch_gui.bat` / `orch.bat` are end-user launchers. Keep them
ASCII with CRLF line endings, and `setup.ps1` / `uninstall.ps1` UTF-8 **with BOM** + CRLF (PowerShell 5.1 reads BOM-less
files as ANSI, which garbles their Thai messages); `test_11_install` checks both. The installer scripts must only call `orch.py`
verbs (`init`, `stop-engine`, `install-startup --apply`) and contain no Orch logic of their own. The user-facing docs are in Thai: `README.md`,
`docs/MANUAL.md` (usage/config) and `docs/TECHNICAL.md` (internals, constants, exit codes, deviations from SPEC).
Update them when behavior, flags or constants change.

Tests are stdlib `unittest` (pytest is not installed). Each test gets its own temp dir + `ORCH_DB`;
`ORCH_TEST_TMP` overrides the temp root.
`tests/test_12_stress.py` (SPEC §11 SQLite contention: 3 engines + GUI-style poller + CLI writers) runs only
with `ORCH_STRESS=1`; it checks queue seriality by ordering on `run_log` ids, because timestamps only have 1 s resolution.
Engine tests (`test_08_*`) start `orch.py engine` as a real
subprocess and stop it with CTRL_BREAK (Windows) / SIGTERM; the engine imports `socket`, so sandboxes that
block Winsock make them fail.

## Architecture (orch.py, single file, sections in this order)

- **Time**: all timestamps are local naive `YYYY-MM-DD HH:MM:SS` strings, compared as strings in SQL.
- **DB**: `connect()` — one connection per thread, autocommit (`isolation_level=None`), WAL, auto-creates the
  schema via `PRAGMA user_version`. All writes go through `with tx(con):` (`BEGIN IMMEDIATE`); `tx` is not
  re-entrant, so internal helpers prefixed `_` (e.g. `_enqueue`, `_fire_schedule`) expect the caller to hold
  the transaction. The custom cursor closes itself after `fetchone()` so long-lived connections don't stay
  pinned to an old WAL snapshot.
- **Schedules**: hand-written `CronExpr`, `parse_every`, `next_fire` (`every` anchors on the previous fire).
- **Jobs**: `create_job` / `update_job` / `set_enabled` / `delete_job` validate everything (params per
  type, `after_job` existence + cycle check) before/inside one transaction, so a failure leaves nothing behind.
  Deleting a job referenced by another job's `after_job` is refused.
- **Runs**: `_enqueue` applies dedup (`manual`/`retry` bypass it); `claim_next` does a read-only pre-check,
  then claims atomically. Queue order: `priority DESC, scheduled_at, id`, with `not_before` holding retries back.
- **Executor**: `build_command` → `execute_process` (Popen in a new process group/session; a reader thread
  pushes output lines to a `queue.Queue`, and the monitor loop writes them to `run_log` in batches, so the
  reader thread never touches SQLite). `kill_tree` = `taskkill /T /F` then `WinJob.terminate()` (a Job Object that
  catches descendants orphaned from the tree; no KILL_ON_JOB_CLOSE, so a normal exit or an engine crash kills nothing), or `killpg` on POSIX.
  Any unexpected exception while supervising kills the tree; `process_one` then finalizes the run as `engine error`,
  so the queue stays serial. `sql`/`http` run as
  `orch.py _exec <type>` with params passed in the `ORCH_PARAMS` env var (never argv).
- **Lifecycle**: `run_job` → `finalize` (Succeeded / Cancelled / Failed+retry run / Dead, then fires
  dependent `after_job` runs on Succeeded/Dead). A graceful shutdown uses `requeue` (back to Pending, attempt not counted).
- **Engine** (`run_engine`): lock → `recover` (Running runs of a dead engine → "engine lost", no auto-kill)
  → `fire_startup` → `SchedulerThread` (own connection: schedule tick every 1 s, lock heartbeat every 5 s,
  hourly prune) + main-thread worker loop (`process_one`). Logs go to stdout and `logs/engine-<queue>.log`
  next to the DB. Graceful stop = `stop` event, set by a signal or by `stop-engine`: `request_engine_stop` sets
  `engine_lock.stop_requested` (schema v2; `init_db` migrates v1 in place), and the scheduler thread's
  `check_stop` sees it within 1 s. A pythonw engine has no console, so `stop-engine` is its only graceful stop.
- **install-startup**: `build_startup_script` generates PowerShell `Register-ScheduledTask` (never `schtasks`: its
  72-hour ExecutionTimeLimit would kill the engine). The script launches `pythonw.exe orch.py --db <abs path> engine`,
  so a SYSTEM/boot engine uses the same DB. Besides the boot/logon trigger, a second trigger repeats every
  `ENGINE_RELAUNCH_MIN` (5) minutes to relaunch a dead engine (IgnoreNew + engine lock make that harmless).
- **CLI**: argparse verbs mirror `schtasks`; `OrchError` → exit code 2; engine lock held → exit code 3; lock lost while running → exit code 4.

## GUI (orch_gui.py)

tkinter; must only call functions in `orch.py`, with no business logic of its own. `python orch_gui.py [--db PATH]`.
- The 2-second refresh uses `sync_tree()`, which updates Treeview rows in place keyed by stable iids
  (`job-<id>`, run id), so selection and scroll position survive a refresh. `App.refresh()` does one pass;
  `_tick()` owns the `after()` loop, so calling `refresh()` never starts a second loop.
- `App.error` / `App.confirm` wrap the messageboxes, and `App.call()` turns `OrchError` / `sqlite3.Error` into `error()`. Tests
  override these and drive the App without `mainloop` (`auto_refresh=False`).
- When editing a job, the dialog passes `schedules=None` if the schedule text is unchanged, so `next_run_at` isn't reset.
