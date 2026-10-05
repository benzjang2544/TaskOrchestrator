import sqlite3
import threading
import unittest
from unittest import mock

from helpers import DBTestCase, orch, py_cmd, wait_until


def exit_cmd(code):
    return {"command": py_cmd(f"import sys; print('exit {code}'); sys.exit({code})")}


class TestFinalize(DBTestCase):
    def drain(self, queue="default", timeout=15):
        """Run the queue in-process until nothing is Pending (waiting through not_before)."""
        stop = threading.Event()
        def pending():
            return self.con.execute("SELECT COUNT(*) FROM job_run WHERE queue=? AND status IN ('Pending','Running')",
                                    (queue,)).fetchone()[0]
        def step():
            orch.process_one(self.con, queue, stop)
            return pending() == 0
        wait_until(step, timeout, 0.1)

    def runs(self, job=None):
        return orch.list_runs(self.con, job=job, limit=100)[::-1]

    def test_success(self):
        orch.create_job(self.con, "a", "command", exit_cmd(0))
        orch.enqueue(self.con, "a")
        self.drain()
        r = self.runs()[0]
        self.assertEqual((r["status"], r["exit_code"], r["error"]), ("Succeeded", 0, None))
        self.assertIsNotNone(r["finished_at"])

    # --- Test Plan #2: retry with backoff ---
    def test_retry_backoff_then_dead(self):
        orch.create_job(self.con, "a", "command", exit_cmd(3), max_retry=2, retry_delay_sec=1, retry_backoff=2)
        orch.enqueue(self.con, "a")
        self.drain(timeout=20)
        rs = self.runs()
        self.assertEqual([r["status"] for r in rs], ["Failed", "Failed", "Dead"])
        self.assertEqual([r["attempt"] for r in rs], [1, 2, 3])
        self.assertEqual([r["trigger"] for r in rs], ["manual", "retry", "retry"])
        self.assertEqual([r["retry_of"] for r in rs], [None, rs[0]["id"], rs[1]["id"]])
        self.assertTrue(all(r["exit_code"] == 3 for r in rs))
        self.assertEqual(len({r["scheduled_at"] for r in rs}), 1)  # keeps original scheduled_at
        gaps = [(orch.parse_ts(rs[i + 1]["not_before"]) - orch.parse_ts(rs[i]["finished_at"])).total_seconds()
                for i in range(2)]
        self.assertEqual(gaps, [1.0, 2.0])
        for i in range(2):
            self.assertGreaterEqual(rs[i + 1]["started_at"], rs[i + 1]["not_before"])

    def test_retry_does_not_block_queue(self):
        orch.create_job(self.con, "bad", "command", exit_cmd(1), max_retry=1, retry_delay_sec=3600, priority=9)
        orch.create_job(self.con, "good", "command", exit_cmd(0), priority=1)
        orch.enqueue(self.con, "bad")
        stop = threading.Event()
        orch.process_one(self.con, "default", stop)  # bad fails, retry waits an hour
        orch.enqueue(self.con, "good")
        self.assertTrue(orch.process_one(self.con, "default", stop))
        self.assertEqual(self.runs("good")[0]["status"], "Succeeded")
        self.assertEqual(self.runs("bad")[-1]["status"], "Pending")

    def test_timeout_is_failure(self):
        orch.create_job(self.con, "a", "python", {"file": "-c", "args": ["import time; time.sleep(30)"]},
                        timeout_sec=1)
        orch.enqueue(self.con, "a")
        self.drain()
        r = self.runs()[0]
        self.assertEqual(r["status"], "Dead")
        self.assertIn("timeout", r["error"])

    def test_config_error_and_deleted_definition(self):
        orch.create_job(self.con, "a", "command", exit_cmd(0))
        self.con.execute("UPDATE job_def SET params='{}' WHERE name='a'")  # broken params
        orch.enqueue(self.con, "a")
        self.drain()
        self.assertIn("config error", self.runs()[0]["error"])
        rid, _ = orch.enqueue(self.con, "a")
        orch.delete_job(self.con, "a")
        self.drain()
        self.assertEqual((self.run_row(rid)["status"], self.run_row(rid)["error"]), ("Dead", "definition deleted"))

    def test_disabled_skips_unless_manual(self):
        orch.create_job(self.con, "a", "command", exit_cmd(0), enabled=0, dedup_policy="allow")
        r1, _ = orch.enqueue(self.con, "a", "schedule")
        r2, _ = orch.enqueue(self.con, "a", "manual")
        self.drain()
        self.assertEqual(self.run_row(r1)["status"], "Skipped")
        self.assertEqual(self.run_row(r2)["status"], "Succeeded")

    def test_engine_error_kills_child_and_keeps_queue_serial(self):
        orch.create_job(self.con, "long", "python", {"file": "-c", "args": ["import time; time.sleep(30)"]},
                        priority=9)
        orch.create_job(self.con, "next", "command", exit_cmd(0), priority=1)
        rid, _ = orch.enqueue(self.con, "long")
        nid, _ = orch.enqueue(self.con, "next")
        real_flush, calls = orch._flush_logs, []

        def flaky_flush(*a):
            calls.append(1)
            if len(calls) == 4:  # a DB failure while the child is running
                raise sqlite3.OperationalError("database is locked")
            return real_flush(*a)
        with mock.patch.object(orch, "_flush_logs", side_effect=flaky_flush), \
                self.assertLogs("orch", "ERROR") as logged:
            self.assertTrue(orch.process_one(self.con, "default", threading.Event()))
        self.assertIn("engine error", logged.output[0])
        row = self.run_row(rid)
        self.assertEqual(row["status"], "Dead")
        self.assertIn("engine error", row["error"])
        self.assertIn("database is locked", row["error"])
        self.assertFalse(orch.pid_alive(row["pid"]))  # child was not left running
        orch.process_one(self.con, "default", threading.Event())
        self.assertEqual(self.status(nid), "Succeeded")

    # --- Test Plan #4: dependency chain ---
    def test_chain_on_success(self):
        orch.create_job(self.con, "A", "command", exit_cmd(0))
        orch.create_job(self.con, "B", "command", exit_cmd(0), after_job="A")
        orch.create_job(self.con, "C", "command", exit_cmd(0), after_job="B", queue="other")
        rid, _ = orch.enqueue(self.con, "A")
        self.drain()
        b = self.runs("B")
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0]["trigger"], f"after:A#{rid}")
        self.assertEqual(b[0]["status"], "Succeeded")
        c = self.runs("C")  # different queue: queued, not run by this queue's worker
        self.assertEqual((c[0]["queue"], c[0]["status"], c[0]["trigger"]), ("other", "Pending", f"after:B#{b[0]['id']}"))

    def test_chain_not_triggered_on_failure(self):
        orch.create_job(self.con, "A", "command", exit_cmd(1), max_retry=1, retry_delay_sec=0)
        orch.create_job(self.con, "B", "command", exit_cmd(0), after_job="A")
        orch.create_job(self.con, "F", "command", exit_cmd(0), after_job="A", after_on="failure")
        orch.create_job(self.con, "W", "command", exit_cmd(0), after_job="A", after_on="always")
        orch.enqueue(self.con, "A")
        self.drain()
        self.assertEqual(self.runs("B"), [])
        self.assertEqual(len(self.runs("F")), 1)  # once, on Dead only (not on the intermediate Failed)
        self.assertEqual(len(self.runs("W")), 1)
        self.assertIn("#2", self.runs("F")[0]["trigger"])

    def test_chain_not_on_cancel_and_dedup_applies(self):
        orch.create_job(self.con, "A", "command", exit_cmd(0))
        orch.create_job(self.con, "B", "command", exit_cmd(0), after_job="A", after_on="always")
        rid, _ = orch.enqueue(self.con, "A")
        orch.claim_next(self.con, "default")
        orch.finalize(self.con, rid, "cancelled", None, "cancelled by user")
        self.assertEqual(self.runs("B"), [])
        orch.set_paused(self.con, "default", True)
        for _ in range(2):
            r, _ = orch.enqueue(self.con, "A", "manual")
            self.con.execute("UPDATE job_run SET status='Running' WHERE id=?", (r,))
            orch.finalize(self.con, r, "exited", 0)
        self.assertEqual([x["status"] for x in self.runs("B")], ["Pending", "Skipped"])  # B's dedup=skip


if __name__ == "__main__":
    unittest.main()
