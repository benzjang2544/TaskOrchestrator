"""SPEC §11 risk: SQLite lock contention with several engines + GUI-like reader + CLI writers.

Slow (~40 s): runs only with ORCH_STRESS=1. ORCH_STRESS_SECONDS changes the load duration (default 30).
"""
import glob
import os
import subprocess
import threading
import time
import unittest

from helpers import DBTestCase, ORCH_PY, PY, orch

QUEUES = ("q1", "q2", "q3")
NOISY_LINES = 3000
BAD_LOG_MARKERS = ("database is locked", "worker error", "scheduler tick failed", "engine error", "Traceback")


@unittest.skipUnless(os.environ.get("ORCH_STRESS") == "1", "set ORCH_STRESS=1 to run the stress test")
class TestStress(DBTestCase):
    def test_multi_engine_gui_cli_contention(self):
        seconds = float(os.environ.get("ORCH_STRESS_SECONDS", "30"))
        for q in QUEUES:
            orch.create_job(self.con, f"noisy-{q}", "python",
                            {"file": "-c", "args": [f"for i in range({NOISY_LINES}): print('line', i, 'ข้อความ')"]},
                            ["every: 1s"], queue=q, priority=5)
            orch.create_job(self.con, f"short-{q}", "python", {"file": "-c", "args": ["pass"]},
                            ["every: 1s"], queue=q, dedup_policy="allow", priority=3)
            orch.create_job(self.con, f"chain-{q}", "python", {"file": "-c", "args": ["print('after')"]},
                            queue=q, after_job=f"noisy-{q}", priority=9)
        engines = [self.start_engine(q) for q in QUEUES]

        stop = threading.Event()
        errors = []

        def gui_like_poller():  # what the GUI does every refresh, but 10x as often
            con = orch.connect()
            try:
                while not stop.is_set():
                    try:
                        runs = orch.list_runs(con, limit=200)
                        orch.engine_states(con)
                        orch.list_jobs(con)
                        if runs:
                            orch.get_logs(con, runs[0]["id"])
                    except Exception as e:
                        errors.append(f"poller: {e!r}")
                    stop.wait(0.2)
            finally:
                con.close()

        def cli_writer():
            env = dict(os.environ, ORCH_DB=self.db)
            i = 0
            while not stop.is_set():
                q = QUEUES[i % len(QUEUES)]
                for args in (["run", f"short-{q}"], ["pause", "--queue", q], ["resume", "--queue", q]):
                    r = subprocess.run([PY, ORCH_PY, *args], capture_output=True, text=True, env=env, timeout=60)
                    if r.returncode != 0:
                        errors.append(f"cli {args}: {r.stderr.strip()}")
                i += 1
                stop.wait(0.5)

        threads = [threading.Thread(target=gui_like_poller), threading.Thread(target=cli_writer)]
        for t in threads:
            t.start()
        time.sleep(seconds)
        stop.set()
        for t in threads:
            t.join(60)
        for q in QUEUES:
            orch.set_paused(self.con, q, False)
        for e in engines:
            self.assertEqual(self.stop_engine(e, timeout=60), 0, self.engine_output(e))

        self.assertEqual(errors, [])
        for path in glob.glob(os.path.join(self.tmp, "logs", "engine-*.log")):
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            for marker in BAD_LOG_MARKERS:
                self.assertNotIn(marker, text, f"{marker!r} in {os.path.basename(path)}")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM job_run WHERE status='Running'").fetchone()[0], 0)

        for q in QUEUES:
            done = self.con.execute(
                "SELECT * FROM job_run WHERE queue=? AND started_at IS NOT NULL AND finished_at IS NOT NULL"
                " ORDER BY started_at, id", (q,)).fetchall()
            self.assertGreater(len([r for r in done if r["status"] == "Succeeded"]), 10, q)
            self.assertEqual({r["status"] for r in done} - {"Succeeded"}, set(), q)
            # serial: never two runs of one queue at once. Timestamps have 1 s resolution, so order
            # exactly by run_log ids (a single autoincrement sequence shared by all writers).
            span = {}
            for r in done:
                ids = [l["id"] for l in orch.get_logs(self.con, r["id"]) if l["level"] != "OUT"]
                span[r["id"]] = (min(ids), max(ids))
            done = sorted(done, key=lambda r: span[r["id"]][0])
            for prev, cur in zip(done, done[1:]):
                if span[cur["id"]][0] < span[prev["id"]][1]:
                    detail = []
                    for r in (prev, cur):
                        logs = orch.get_logs(self.con, r["id"])
                        detail.append(f"{dict(r)}\n  logs: {[(l['ts'], l['level'], l['message'][:60]) for l in logs if l['level'] != 'OUT']}")
                    self.fail(f"{q}: runs overlap\n" + "\n".join(detail))
            self.assertTrue(any(r["job_name"] == f"chain-{q}" for r in done), q)
            for r in done:
                if r["job_name"] == f"noisy-{q}":
                    n = self.con.execute("SELECT COUNT(*) FROM run_log WHERE run_id=? AND level='OUT'",
                                         (r["id"],)).fetchone()[0]
                    self.assertEqual(n, NOISY_LINES, f"run #{r['id']} lost output")


if __name__ == "__main__":
    unittest.main()
