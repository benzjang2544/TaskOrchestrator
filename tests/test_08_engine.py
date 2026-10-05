"""Engine integration tests: the engine runs as a real subprocess."""
import datetime as dt
import os
import sqlite3
import threading
import time
import unittest

from helpers import DBTestCase, orch, py_cmd, wait_until


def sleep_job(sec):
    return {"file": "-c", "args": [f"import time; print('sleeping'); time.sleep({sec})"]}


class TestEngine(DBTestCase):
    def job(self, name, params=None, job_type="python", **kw):
        return orch.create_job(self.con, name, job_type, params or sleep_job(0), **kw)

    def first_log_id(self, rid):
        return self.con.execute("SELECT MIN(id) FROM run_log WHERE run_id=?", (rid,)).fetchone()[0]

    # --- Test Plan #1 (end-to-end) ---
    def test_priority_order_through_engine(self):
        self.cli("pause")
        ids = {}
        for prio in (1, 9, 5):
            self.job(f"p{prio}", priority=prio)
            ids[prio], _ = orch.enqueue(self.con, f"p{prio}")
        eng = self.start_engine()
        time.sleep(1)
        self.assertEqual(self.status(ids[9]), "Pending")  # paused: nothing taken
        self.cli("resume")
        wait_until(lambda: all(self.status(i) == "Succeeded" for i in ids.values()), 20)
        order = sorted(ids, key=lambda p: self.first_log_id(ids[p]))
        self.assertEqual(order, [9, 5, 1])
        self.assertEqual(self.stop_engine(eng), 0)

    # --- Test Plan #3: timeout kills tree, next job still runs ---
    def test_timeout_then_next_job(self):
        pidfile = os.path.join(self.tmp, "child.pid")
        code = ("import subprocess, sys, time; "
                "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                f"open(r'{pidfile}', 'w').write(str(c.pid)); time.sleep(30)")
        self.job("slow", {"file": "-c", "args": [code]}, timeout_sec=6, priority=9)
        self.job("next", priority=1)
        r1, _ = orch.enqueue(self.con, "slow")
        r2, _ = orch.enqueue(self.con, "next")
        self.start_engine()
        wait_until(lambda: self.status(r2) == "Succeeded", 40)
        row = self.run_row(r1)
        self.assertEqual(row["status"], "Dead")
        self.assertIn("timeout", row["error"])
        with open(pidfile) as f:
            child = int(f.read())
        self.assertFalse(orch.pid_alive(row["pid"]))
        wait_until(lambda: not orch.pid_alive(child), 5)

    # --- Test Plan #6: cancel running job ---
    def test_cancel_running_via_cli(self):
        self.job("long", sleep_job(30))
        rid, _ = orch.enqueue(self.con, "long")
        self.start_engine()
        wait_until(lambda: self.run_row(rid)["pid"], 15)
        pid = self.run_row(rid)["pid"]
        t0 = time.time()
        self.assertIn("cancel requested", self.cli("end", str(rid)).stdout)
        wait_until(lambda: self.status(rid) == "Cancelled", 6)
        self.assertLess(time.time() - t0, 4.5)
        self.assertFalse(orch.pid_alive(pid))

    # --- Test Plan #8: engine killed hard, then restarted ---
    def test_recovery_after_hard_kill(self):
        marker = os.path.join(self.tmp, "marker")
        code = (f"import os, time\nif os.path.exists(r'{marker}'): print('second try ok')\n"
                f"else:\n    open(r'{marker}', 'w').close(); time.sleep(60)")
        self.job("j", {"file": "-c", "args": [code]}, max_retry=1, retry_delay_sec=0)
        rid, _ = orch.enqueue(self.con, "j")
        eng = self.start_engine()
        wait_until(lambda: self.run_row(rid)["pid"] and os.path.exists(marker), 30)
        orphan = self.run_row(rid)["pid"]
        eng.kill()  # no chance to clean up
        eng.wait(10)
        try:
            self.assertEqual(self.status(rid), "Running")
            self.assertTrue(orch.pid_alive(orphan))
            eng2 = self.start_engine()  # lock is stale-by-pid? no: heartbeat fresh -> must wait
            rc = eng2.wait(15)
            self.assertEqual(rc, orch.EXIT_LOCKED)  # previous lock heartbeat still fresh
            self.con.execute("UPDATE engine_lock SET heartbeat='2000-01-01 00:00:00'")  # make it stale
            self.start_engine()
            wait_until(lambda: self.status(rid) == "Failed", 30)
            row = self.run_row(rid)
            self.assertEqual(row["error"], "engine lost")
            logs = " ".join(l["message"] for l in orch.get_logs(self.con, rid))
            self.assertIn("still alive", logs)
            retry = self.con.execute("SELECT * FROM job_run WHERE retry_of=?", (rid,)).fetchone()
            self.assertEqual(retry["attempt"], 2)
            wait_until(lambda: self.status(retry["id"]) == "Succeeded", 30)
        finally:
            orch.kill_tree(orphan)

    # --- Test Plan #9: second engine on the same queue ---
    def test_second_engine_refused(self):
        e1 = self.start_engine()
        wait_until(lambda: self.con.execute("SELECT 1 FROM engine_lock WHERE queue='default'").fetchone(), 10)
        e2 = self.start_engine()
        self.assertEqual(e2.wait(15), orch.EXIT_LOCKED)
        self.assertIn("already running", self.engine_output(e2))
        self.assertIsNone(e1.poll())
        self.start_engine("other")  # a different queue is fine
        wait_until(lambda: self.con.execute("SELECT COUNT(*) FROM engine_lock").fetchone()[0] == 2, 10)
        states = {s["queue"]: s for s in orch.engine_states(self.con)}
        self.assertTrue(states["default"]["running"] and states["other"]["running"])
        self.assertEqual(self.stop_engine(e1), 0)
        self.assertIsNone(self.con.execute("SELECT 1 FROM engine_lock WHERE queue='default'").fetchone())

    # --- Test Plan #10: schedules fire while a long job runs ---
    def test_scheduler_thread_independent_of_worker(self):
        self.job("long", sleep_job(8), priority=9)
        orch.create_job(self.con, "tick", "python", sleep_job(0), ["every: 1s"], dedup_policy="allow", priority=1)
        rid, _ = orch.enqueue(self.con, "long")
        self.start_engine()
        wait_until(lambda: self.status(rid) == "Running", 15)

        def ticks_while_running():
            n = self.con.execute("SELECT COUNT(*) FROM job_run WHERE job_name='tick' AND status='Pending'"
                                 " AND scheduled_at > ?", (self.run_row(rid)["started_at"],)).fetchone()[0]
            return n >= 2 and self.status(rid) == "Running"
        wait_until(ticks_while_running, 7)

    # --- Test Plan #12: graceful shutdown returns the run to Pending ---
    def test_graceful_shutdown_requeues(self):
        self.job("long", sleep_job(30))
        rid, _ = orch.enqueue(self.con, "long")
        eng = self.start_engine()
        wait_until(lambda: self.run_row(rid)["pid"], 15)
        pid = self.run_row(rid)["pid"]
        self.assertEqual(self.stop_engine(eng), 0)
        row = self.run_row(rid)
        self.assertEqual((row["status"], row["attempt"], row["pid"], row["started_at"]), ("Pending", 1, None, None))
        self.assertFalse(orch.pid_alive(pid))
        self.assertTrue(any(l["level"] == "WARN" and "Pending" in l["message"] for l in orch.get_logs(self.con, rid)))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM job_run").fetchone()[0], 1)  # no retry created
        self.assertIsNone(self.con.execute("SELECT 1 FROM engine_lock").fetchone())

    def test_stop_engine_via_cli_requeues(self):
        self.assertIn("no running engine", self.cli("stop-engine", check=False).stderr)
        self.job("long", sleep_job(30))
        rid, _ = orch.enqueue(self.con, "long")
        eng = self.start_engine()
        wait_until(lambda: self.run_row(rid)["pid"], 15)
        pid = self.run_row(rid)["pid"]
        r = self.cli("stop-engine", "--wait", "30")
        self.assertIn("engine stopped", r.stdout)
        self.assertEqual(eng.wait(10), 0)
        row = self.run_row(rid)
        self.assertEqual((row["status"], row["attempt"]), ("Pending", 1))
        self.assertFalse(orch.pid_alive(pid))
        self.assertIsNone(self.con.execute("SELECT 1 FROM engine_lock").fetchone())
        self.assertIn("stop requested (stop-engine)", self.engine_output(eng))
        eng = self.start_engine()  # a new engine starts clean (flag not inherited) and runs the job
        wait_until(lambda: self.run_row(rid)["status"] == "Running", 15)
        self.assertEqual(self.stop_engine(eng), 0)

    def test_startup_trigger_and_engine_log_file(self):
        orch.create_job(self.con, "boot", "python", sleep_job(0), ["startup"])
        self.start_engine()
        wait_until(lambda: [r["status"] for r in orch.list_runs(self.con, job="boot")] == ["Succeeded"], 15)
        path = os.path.join(self.tmp, "logs", "engine-default.log")
        wait_until(lambda: os.path.exists(path), 5)
        with open(path, encoding="utf-8") as f:
            self.assertIn("engine for queue 'default' started", f.read())


class TestPlanThroughEngine(DBTestCase):
    """SPEC §9 items that test_06/07/09 cover in-process, repeated with a real engine subprocess."""

    def runs(self, job):
        return self.con.execute("SELECT * FROM job_run WHERE job_name=? ORDER BY id", (job,)).fetchall()

    def final(self, job, n):
        return lambda: len(self.runs(job)) == n and all(r["status"] in orch.FINAL_STATUSES for r in self.runs(job))

    # --- Test Plan #2 ---
    def test_retry_backoff(self):
        orch.create_job(self.con, "r", "command", {"command": py_cmd("import sys; sys.exit(3)")},
                        max_retry=2, retry_delay_sec=1, retry_backoff=2)
        orch.enqueue(self.con, "r")
        self.start_engine()
        wait_until(self.final("r", 3), 30)
        rs = self.runs("r")
        self.assertEqual([r["status"] for r in rs], ["Failed", "Failed", "Dead"])
        self.assertEqual([r["attempt"] for r in rs], [1, 2, 3])
        gaps = [(orch.parse_ts(rs[i + 1]["not_before"]) - orch.parse_ts(rs[i]["finished_at"])).total_seconds()
                for i in range(2)]
        self.assertEqual(gaps, [1.0, 2.0])

    # --- Test Plan #4 ---
    def test_chain(self):
        ok, bad = {"file": "-c", "args": ["pass"]}, {"file": "-c", "args": ["import sys; sys.exit(1)"]}
        orch.create_job(self.con, "A", "python", ok)
        orch.create_job(self.con, "B", "python", ok, after_job="A")
        orch.create_job(self.con, "A2", "python", bad)
        orch.create_job(self.con, "B2", "python", ok, after_job="A2")
        ra, _ = orch.enqueue(self.con, "A")
        orch.enqueue(self.con, "A2")
        self.start_engine()
        wait_until(self.final("B", 1), 30)
        self.assertEqual(self.runs("B")[0]["trigger"], f"after:A#{ra}")
        self.assertEqual(self.runs("B")[0]["status"], "Succeeded")
        wait_until(self.final("A2", 1), 30)
        time.sleep(1.5)
        self.assertEqual(self.runs("A2")[0]["status"], "Dead")
        self.assertEqual(self.runs("B2"), [])

    # --- Test Plan #7 ---
    def test_missed_policies_at_engine_start(self):
        start = orch.fmt_ts(orch.now_dt() - dt.timedelta(seconds=3600 - 30))  # no fire inside the 90 s window
        for name, missed, dedup in (("m-skip", "skip", "skip"), ("m-once", "run_once", "skip"),
                                    ("m-all", "run_all", "allow")):
            orch.create_job(self.con, name, "python", {"file": "-c", "args": ["pass"]}, ["every: 10m"],
                            missed_policy=missed, dedup_policy=dedup)
        self.con.execute("UPDATE schedule SET next_run_at=?", (start,))
        self.start_engine()
        wait_until(lambda: self.con.execute("SELECT COUNT(*) FROM schedule WHERE next_run_at<=?",
                                            (orch.now_str(),)).fetchone()[0] == 0, 15)
        wait_until(self.final("m-all", 6), 30)
        self.assertEqual(len(self.runs("m-skip")), 0)
        self.assertEqual(len(self.runs("m-once")), 1)
        self.assertEqual({r["status"] for r in self.runs("m-all")}, {"Succeeded"})

    # --- Test Plan #11 ---
    def test_sql_and_http(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200 if self.path == "/ok" else 503)
                self.end_headers()
                self.wfile.write(b"pong")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        target = os.path.join(self.tmp, "t.db")
        sqlite3.connect(target).close()
        orch.create_job(self.con, "sql-ok", "sql", {"conn": f"sqlite:{target}",
                                                    "query": "CREATE TABLE t(a); INSERT INTO t VALUES(7); SELECT a FROM t"})
        orch.create_job(self.con, "sql-bad", "sql", {"conn": f"sqlite:{target}", "query": "SELECT * FROM nope"})
        orch.create_job(self.con, "http-ok", "http", {"url": base + "/ok", "env": {"no_proxy": "*"}})
        orch.create_job(self.con, "http-bad", "http", {"url": base + "/down", "env": {"no_proxy": "*"}})
        ids = {n: orch.enqueue(self.con, n)[0] for n in ("sql-ok", "sql-bad", "http-ok", "http-bad")}
        self.start_engine()
        wait_until(lambda: all(self.status(i) in orch.FINAL_STATUSES for i in ids.values()), 30)
        def out(n):
            return "\n".join(l["message"] for l in orch.get_logs(self.con, ids[n]))
        self.assertEqual({n: self.status(i) for n, i in ids.items()},
                         {"sql-ok": "Succeeded", "sql-bad": "Dead", "http-ok": "Succeeded", "http-bad": "Dead"},
                         "\n".join(f"--- {n}\n{out(n)}" for n in ids))
        self.assertIn("(1 row)", out("sql-ok"))
        self.assertIn("no such table", out("sql-bad"))
        self.assertIn("HTTP 200", out("http-ok"))
        self.assertIn("HTTP 503", out("http-bad"))


class TestLockUnit(DBTestCase):
    def test_lock_lifecycle(self):
        orch.acquire_lock(self.con, "q", "h", 1)
        orch.acquire_lock(self.con, "q", "h", 1)  # re-entrant for the same owner
        with self.assertRaisesRegex(orch.OrchError, "already running"):
            orch.acquire_lock(self.con, "q", "h", 2)
        self.assertTrue(orch.heartbeat_lock(self.con, "q", "h", 1))
        self.con.execute("UPDATE engine_lock SET heartbeat='2000-01-01 00:00:00'")
        orch.acquire_lock(self.con, "q", "h", 2)  # stale -> taken over
        self.assertFalse(orch.heartbeat_lock(self.con, "q", "h", 1))  # old owner notices
        orch.release_lock(self.con, "q", "h", 1)  # not ours anymore: no-op
        self.assertIsNotNone(self.con.execute("SELECT 1 FROM engine_lock").fetchone())
        orch.release_lock(self.con, "q", "h", 2)
        self.assertIsNone(self.con.execute("SELECT 1 FROM engine_lock").fetchone())

    def test_stop_request(self):
        with self.assertRaisesRegex(orch.OrchError, "no running engine"):
            orch.request_engine_stop(self.con, "q")
        orch.acquire_lock(self.con, "q", "h", 1)
        self.assertFalse(orch.engine_stop_requested(self.con, "q", "h", 1))
        self.assertEqual(orch.request_engine_stop(self.con, "q")["pid"], 1)
        self.assertTrue(orch.engine_stop_requested(self.con, "q", "h", 1))
        self.assertFalse(orch.engine_stop_requested(self.con, "q", "h", 2))  # only the lock owner's flag
        self.con.execute("UPDATE engine_lock SET heartbeat='2000-01-01 00:00:00'")
        with self.assertRaisesRegex(orch.OrchError, "no running engine"):  # stale lock = dead engine
            orch.request_engine_stop(self.con, "q")
        orch.acquire_lock(self.con, "q", "h", 2)  # takeover resets the flag
        self.assertFalse(orch.engine_stop_requested(self.con, "q", "h", 2))


if __name__ == "__main__":
    unittest.main()
