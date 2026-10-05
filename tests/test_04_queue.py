import json
import unittest

from helpers import DBTestCase, orch


class TestQueue(DBTestCase):
    def job(self, name, **kw):
        return orch.create_job(self.con, name, "command", {"command": "echo hi"}, **kw)

    def claim_all(self, queue="default"):
        out = []
        while (r := orch.claim_next(self.con, queue)) is not None:
            out.append(r)
        return out

    # --- Test Plan #1: priority ordering + FIFO, pause/resume ---
    def test_priority_order_and_fifo(self):
        orch.set_paused(self.con, "default", True)
        for name, prio in (("p1", 1), ("p9", 9), ("p5", 5), ("p5b", 5)):
            self.job(name, priority=prio)
            orch.enqueue(self.con, name, "manual")
        self.assertIsNone(orch.claim_next(self.con, "default"))  # paused
        orch.set_paused(self.con, "default", False)
        self.assertEqual([r["job_name"] for r in self.claim_all()], ["p9", "p5", "p5b", "p1"])

    def test_scheduled_at_before_id(self):
        self.job("a", dedup_policy="allow")
        late, _ = orch.enqueue(self.con, "a", "schedule", scheduled_at="2026-01-01 10:00:00")
        early, _ = orch.enqueue(self.con, "a", "schedule", scheduled_at="2026-01-01 09:00:00")
        self.assertEqual([r["id"] for r in self.claim_all()], [early, late])

    def test_claim_sets_running_and_skips_not_before(self):
        self.job("a")
        self.job("b")
        ra, _ = orch.enqueue(self.con, "a")
        self.con.execute("UPDATE job_run SET not_before='2999-01-01 00:00:00', priority=9 WHERE id=?", (ra,))
        rb, _ = orch.enqueue(self.con, "b")
        r = orch.claim_next(self.con, "default")
        self.assertEqual(r["id"], rb)  # a is held back by not_before even with higher priority
        self.assertEqual(r["status"], "Running")
        self.assertIsNotNone(r["started_at"])
        self.assertIsNotNone(r["heartbeat"])
        self.assertIsNone(orch.claim_next(self.con, "default"))

    def test_queues_are_separate(self):
        self.job("a", queue="q1")
        self.job("b", queue="q2")
        orch.enqueue(self.con, "a")
        orch.enqueue(self.con, "b")
        orch.set_paused(self.con, "q1", True)
        self.assertIsNone(orch.claim_next(self.con, "q1"))
        self.assertEqual(orch.claim_next(self.con, "q2")["job_name"], "b")

    # --- Test Plan #5: dedup ---
    def test_dedup_skip(self):
        self.job("a", dedup_policy="skip")
        r1, s1 = orch.enqueue(self.con, "a", "schedule")
        r2, s2 = orch.enqueue(self.con, "a", "schedule")
        self.assertEqual((s1, s2), ("Pending", "Skipped"))
        row = self.run_row(r2)
        self.assertEqual(row["status"], "Skipped")
        self.assertIn(f"#{r1}", row["error"])
        self.assertIsNotNone(row["finished_at"])

    def test_dedup_coalesce(self):
        self.job("a", dedup_policy="coalesce")
        r1, _ = orch.enqueue(self.con, "a", "schedule")
        r2, s2 = orch.enqueue(self.con, "a", "schedule")
        self.assertEqual(s2, "Pending")
        self.assertEqual(self.run_row(r1)["status"], "Skipped")
        self.assertEqual(self.run_row(r1)["error"], f"coalesced by #{r2}")
        self.assertEqual(self.run_row(r2)["status"], "Pending")

    def test_dedup_allow_and_manual_bypass(self):
        self.job("a", dedup_policy="allow")
        self.job("b", dedup_policy="skip")
        orch.enqueue(self.con, "a", "schedule")
        self.assertEqual(orch.enqueue(self.con, "a", "schedule")[1], "Pending")
        orch.enqueue(self.con, "b", "schedule")
        self.assertEqual(orch.enqueue(self.con, "b", "manual")[1], "Pending")
        self.assertEqual(orch.enqueue(self.con, "b", "after:x#1")[1], "Skipped")

    def test_dedup_only_considers_pending(self):
        self.job("a", dedup_policy="skip")
        orch.enqueue(self.con, "a", "schedule")
        orch.claim_next(self.con, "default")  # now Running
        self.assertEqual(orch.enqueue(self.con, "a", "schedule")[1], "Pending")

    def test_cancel(self):
        self.job("a")
        rid, _ = orch.enqueue(self.con, "a")
        self.assertEqual(orch.cancel_run(self.con, rid), "Cancelled")
        self.assertEqual(self.run_row(rid)["status"], "Cancelled")
        with self.assertRaisesRegex(orch.OrchError, "already finished"):
            orch.cancel_run(self.con, rid)
        rid2, _ = orch.enqueue(self.con, "a")
        orch.claim_next(self.con, "default")
        self.assertEqual(orch.cancel_run(self.con, rid2), "cancel requested")
        self.assertEqual(self.run_row(rid2)["cancel_requested"], 1)
        self.assertEqual(self.run_row(rid2)["status"], "Running")
        with self.assertRaises(orch.OrchError):
            orch.cancel_run(self.con, 999)


class TestQueueCli(DBTestCase):
    def test_run_runs_end_pause_resume_logs(self):
        self.cli("create", "a", "--type", "command", "--command", "echo hi")
        self.cli("pause")
        self.assertTrue(orch.is_paused(self.con, "default"))
        self.assertIn("queued run #1", self.cli("run", "a").stdout)
        runs = json.loads(self.cli("runs", "--json").stdout)
        self.assertEqual((runs[0]["id"], runs[0]["status"], runs[0]["trigger"]), (1, "Pending", "manual"))
        self.assertIn("Pending", self.cli("runs", "--status", "Pending").stdout)
        self.assertEqual(json.loads(self.cli("runs", "--job", "zzz", "--json").stdout), [])
        self.assertIn("Cancelled", self.cli("end", "1").stdout)
        self.assertIn("cancelled while pending", self.cli("logs", "1").stdout)
        self.assertIn("cancelled while pending", self.cli("logs", "1", "--follow").stdout)
        self.cli("resume")
        self.assertFalse(orch.is_paused(self.con, "default"))
        self.assertEqual(self.cli("end", "1", check=False).returncode, 2)


if __name__ == "__main__":
    unittest.main()
