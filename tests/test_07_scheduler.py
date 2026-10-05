import datetime as dt
import threading
import unittest

from helpers import DBTestCase, orch, wait_until

CMD = {"command": "echo hi"}


class TestScheduler(DBTestCase):
    def runs(self, job):
        return self.con.execute("SELECT * FROM job_run WHERE job_name=? ORDER BY scheduled_at, id", (job,)).fetchall()

    def set_next(self, job, when):
        self.con.execute("UPDATE schedule SET next_run_at=? WHERE job_id=(SELECT id FROM job_def WHERE name=?)",
                         (orch.fmt_ts(when), job))

    def sched(self, job):
        return self.con.execute("SELECT * FROM schedule WHERE job_id=(SELECT id FROM job_def WHERE name=?)",
                                (job,)).fetchone()

    # --- Test Plan #7: missed schedule policies ---
    def test_missed_policies(self):
        now = orch.now_dt()
        # 1h back, offset so no fire lands inside the 90s on-time window: fires at -59.5m ... -9.5m (6 missed)
        start = now - dt.timedelta(seconds=3600 - 30)
        for name, missed, dedup in (("skip", "skip", "skip"), ("once", "run_once", "skip"),
                                    ("all", "run_all", "allow")):
            orch.create_job(self.con, name, "command", CMD, ["every: 10m"], missed_policy=missed, dedup_policy=dedup)
            self.set_next(name, start)
        orch.scheduler_tick(self.con, "default", now)
        self.assertEqual(len(self.runs("skip")), 0)
        once = self.runs("once")
        self.assertEqual(len(once), 1)
        self.assertEqual(once[0]["scheduled_at"], orch.fmt_ts(start + dt.timedelta(minutes=50)))  # the last one
        allr = self.runs("all")
        self.assertEqual(len(allr), 6)
        self.assertEqual([r["scheduled_at"] for r in allr],
                         [orch.fmt_ts(start + dt.timedelta(minutes=10 * i)) for i in range(6)])
        self.assertTrue(all(r["trigger"] == "every" for r in allr))
        s = self.sched("all")
        self.assertEqual(s["next_run_at"], orch.fmt_ts(start + dt.timedelta(minutes=60)))  # anchored, no drift
        self.assertEqual(s["last_fire_at"], orch.fmt_ts(start + dt.timedelta(minutes=50)))

    def test_run_all_with_dedup_skip_is_cut(self):
        now = orch.now_dt()
        orch.create_job(self.con, "a", "command", CMD, ["every: 10m"], missed_policy="run_all")
        self.set_next("a", now - dt.timedelta(seconds=3600 - 30))
        orch.scheduler_tick(self.con, "default", now)
        self.assertEqual([r["status"] for r in self.runs("a")], ["Pending"] + ["Skipped"] * 5)

    def test_on_time_fire_always_enqueued(self):
        now = orch.now_dt()
        orch.create_job(self.con, "a", "command", CMD, ["every: 1h"], missed_policy="skip")
        self.set_next("a", now - dt.timedelta(seconds=30))
        self.assertEqual(len(orch.scheduler_tick(self.con, "default", now)), 1)
        self.assertEqual(orch.scheduler_tick(self.con, "default", now), [])  # advanced

    def test_cap_recomputes_from_now(self):
        now = orch.now_dt().replace(second=10)
        orch.create_job(self.con, "a", "command", CMD, ["cron: * * * * *"], missed_policy="run_once")
        self.set_next("a", now - dt.timedelta(days=2))
        orch.scheduler_tick(self.con, "default", now)
        self.assertEqual(len(self.runs("a")), 1)
        self.assertEqual(self.sched("a")["next_run_at"],
                         orch.fmt_ts(now.replace(second=0) + dt.timedelta(minutes=1)))

    def test_at_fires_once_then_disables(self):
        now = orch.now_dt()
        orch.create_job(self.con, "a", "command", CMD, [("at", orch.fmt_ts(now + dt.timedelta(hours=1)))])
        orch.scheduler_tick(self.con, "default", now + dt.timedelta(hours=1, seconds=5))
        s = self.sched("a")
        self.assertEqual((s["next_run_at"], s["enabled"]), (None, 0))
        self.assertEqual(len(self.runs("a")), 1)
        orch.scheduler_tick(self.con, "default", now + dt.timedelta(hours=2))
        self.assertEqual(len(self.runs("a")), 1)

    def test_disabled_job_and_other_queue_not_fired(self):
        now = orch.now_dt()
        orch.create_job(self.con, "off", "command", CMD, ["every: 1m"], enabled=0)
        orch.create_job(self.con, "q2", "command", CMD, ["every: 1m"], queue="q2")
        self.set_next("off", now)
        self.set_next("q2", now)
        self.assertEqual(orch.scheduler_tick(self.con, "default", now), [])
        self.assertEqual(len(orch.scheduler_tick(self.con, "q2", now)), 1)

    def test_startup_trigger(self):
        orch.create_job(self.con, "a", "command", CMD, ["startup"])
        orch.create_job(self.con, "b", "command", CMD, ["startup"], queue="q2")
        orch.create_job(self.con, "c", "command", CMD, ["every: 1h"])
        ids = orch.fire_startup(self.con, "default")
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.run_row(ids[0])["trigger"], "startup")
        self.assertEqual(self.run_row(ids[0])["job_name"], "a")

    def test_scheduler_thread_fires(self):
        orch.create_job(self.con, "a", "command", CMD, ["every: 1s"], dedup_policy="allow")
        stop = threading.Event()
        beats = []
        th = orch.SchedulerThread("default", stop, lock_heartbeat=lambda c: beats.append(1), lock_every=0.5)
        th.start()
        try:
            wait_until(lambda: len(self.runs("a")) >= 2 and beats, 8)
        finally:
            stop.set()
            th.join(5)
        self.assertFalse(th.is_alive())

    def test_prune(self):
        orch.create_job(self.con, "a", "command", CMD, dedup_policy="allow")
        old, _ = orch.enqueue(self.con, "a")
        new, _ = orch.enqueue(self.con, "a")
        pend, _ = orch.enqueue(self.con, "a")
        self.con.execute("UPDATE job_run SET status='Succeeded', finished_at='2000-01-01 00:00:00' WHERE id=?", (old,))
        self.con.execute("UPDATE job_run SET status='Succeeded', finished_at=? WHERE id=?", (orch.now_str(), new))
        orch.add_log(self.con, old, "INFO", "x")
        orch.add_log(self.con, new, "INFO", "y")
        self.assertIn("pruned 1", self.cli("prune", "--days", "30").stdout)
        self.assertEqual([r[0] for r in self.con.execute("SELECT id FROM job_run ORDER BY id")], [new, pend])
        self.assertEqual([r[0] for r in self.con.execute("SELECT run_id FROM run_log")], [new])


if __name__ == "__main__":
    unittest.main()
