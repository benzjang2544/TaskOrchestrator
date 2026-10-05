import json
import unittest

from helpers import DBTestCase, orch


def cmd(c="echo hi"):
    return {"command": c}


class TestJobCrud(DBTestCase):
    def count(self, table):
        return self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_create_with_defaults_and_schedules(self):
        j = orch.create_job(self.con, "a", "command", cmd(), ["cron: */5 * * * *", ("every", "10m"), "startup"])
        self.assertEqual(j["queue"], "default")
        self.assertEqual(j["priority"], 5)
        self.assertEqual(j["dedup_policy"], "skip")
        self.assertEqual(j["missed_policy"], "run_once")
        self.assertEqual(j["timeout_sec"], 3600)
        self.assertEqual([s["kind"] for s in j["schedules"]], ["cron", "every", "startup"])
        self.assertIsNone(j["schedules"][2]["next_run_at"])
        self.assertIsNotNone(j["next_run_at"])

    def test_update_only_given_fields_and_replace_schedules(self):
        orch.create_job(self.con, "a", "command", cmd(), ["every: 1h"], priority=7, description="x")
        j = orch.update_job(self.con, "a", max_retry=3)
        self.assertEqual((j["priority"], j["max_retry"], j["description"]), (7, 3, "x"))
        self.assertEqual(len(j["schedules"]), 1)
        j = orch.update_job(self.con, "a", schedules=["cron: 0 1 * * *", "cron: 0 2 * * *"])
        self.assertEqual([s["expr"] for s in j["schedules"]], ["0 1 * * *", "0 2 * * *"])
        j = orch.update_job(self.con, "a", schedules=[])
        self.assertEqual(j["schedules"], [])

    def test_enable_disable_delete(self):
        orch.create_job(self.con, "a", "command", cmd(), ["every: 1h"])
        self.assertEqual(orch.set_enabled(self.con, "a", False)["enabled"], 0)
        # stale next_run_at gets restarted from now on enable
        self.con.execute("UPDATE schedule SET next_run_at='2000-01-01 00:00:00'")
        j = orch.set_enabled(self.con, "a", True)
        self.assertEqual(j["enabled"], 1)
        self.assertGreater(j["next_run_at"], orch.now_str())
        orch.delete_job(self.con, "a")
        self.assertEqual(self.count("job_def"), 0)
        self.assertEqual(self.count("schedule"), 0)  # cascade
        with self.assertRaises(orch.OrchError):
            orch.delete_job(self.con, "a")

    def test_delete_referenced_job_refused(self):
        orch.create_job(self.con, "a", "command", cmd())
        orch.create_job(self.con, "b", "command", cmd(), after_job="a")
        with self.assertRaisesRegex(orch.OrchError, "referenced"):
            orch.delete_job(self.con, "a")
        orch.update_job(self.con, "b", after_job="")
        orch.delete_job(self.con, "a")

    # --- Test Plan #13: validation leaves nothing half-created ---
    def test_validation_errors_leave_no_partial_job(self):
        orch.create_job(self.con, "a", "command", cmd())
        cases = [
            (("a", "command", cmd()), {}, "already exists"),
            (("b", "command", cmd(), ["cron: 61 * * * *"]), {}, "cron"),
            (("b", "command", cmd(), ["at: 2000-01-01 00:00"]), {}, "past"),
            (("b", "command", {}), {}, "command"),
            (("b", "python", {"args": "x"}), {}, "file"),
            (("b", "powershell", {}), {}, "exactly one"),
            (("b", "sql", {"conn": "sqlite:x.db"}), {}, "query"),
            (("b", "http", {"url": "ftp://x"}), {}, "http"),
            (("b", "http", {"url": "http://x", "expect": "abc"}), {}, "expect"),
            (("b", "nope", {}), {}, "unknown job type"),
            (("b", "command", cmd()), {"after_job": "zzz"}, "does not exist"),
            (("b", "command", cmd()), {"dedup_policy": "maybe"}, "dedup_policy"),
            (("b", "command", cmd()), {"timeout_sec": -1}, "timeout_sec"),
            (("", "command", cmd()), {}, "name"),
        ]
        for args, kw, msg in cases:
            with self.assertRaisesRegex(orch.OrchError, msg, msg=str(args)):
                orch.create_job(self.con, *args, **kw)
        self.assertEqual(self.count("job_def"), 1)
        self.assertEqual(self.count("schedule"), 0)

    def test_cycle_detection(self):
        orch.create_job(self.con, "a", "command", cmd())
        orch.create_job(self.con, "b", "command", cmd(), after_job="a")
        orch.create_job(self.con, "c", "command", cmd(), after_job="b")
        with self.assertRaisesRegex(orch.OrchError, "cycle"):
            orch.update_job(self.con, "a", after_job="c")
        with self.assertRaisesRegex(orch.OrchError, "itself"):
            orch.update_job(self.con, "a", after_job="a")
        self.assertIsNone(orch.get_job(self.con, "a")["after_job"])

    def test_failed_update_changes_nothing(self):
        orch.create_job(self.con, "a", "command", cmd(), ["every: 1h"], priority=3)
        with self.assertRaises(orch.OrchError):
            orch.update_job(self.con, "a", priority=9, schedules=["cron: bad"])
        with self.assertRaises(orch.OrchError):
            orch.update_job(self.con, "a", priority=9, params={})
        j = orch.get_job(self.con, "a")
        self.assertEqual(j["priority"], 3)
        self.assertEqual(j["schedules"][0]["expr"], "1h")

    def test_update_reenable_restarts_stale_schedules(self):
        # same rule as set_enabled: time spent disabled is not "missed" (the GUI edit dialog uses update_job)
        orch.create_job(self.con, "a", "command", cmd(), ["every: 1h"], enabled=0)
        self.con.execute("UPDATE schedule SET next_run_at='2000-01-01 00:00:00'")
        j = orch.update_job(self.con, "a", priority=4)  # still disabled: untouched
        self.assertEqual(j["schedules"][0]["next_run_at"], "2000-01-01 00:00:00")
        j = orch.update_job(self.con, "a", enabled=1)
        self.assertGreater(j["schedules"][0]["next_run_at"], orch.now_str())
        self.con.execute("UPDATE schedule SET next_run_at='2000-01-01 00:00:00'")
        j = orch.update_job(self.con, "a", description="x")  # already enabled: untouched
        self.assertEqual(j["schedules"][0]["next_run_at"], "2000-01-01 00:00:00")

    def test_update_empty_description_clears_it(self):
        orch.create_job(self.con, "a", "command", cmd(), description="x")
        self.assertEqual(orch.update_job(self.con, "a", description=None)["description"], "x")
        self.assertIsNone(orch.update_job(self.con, "a", description="")["description"])
        self.assertIsNone(orch.create_job(self.con, "b", "command", cmd(), description="")["description"])

    def test_queue_change_moves_pending_runs(self):
        orch.create_job(self.con, "a", "command", cmd(), dedup_policy="allow")
        done, _ = orch.enqueue(self.con, "a")
        orch.cancel_run(self.con, done)
        pending, _ = orch.enqueue(self.con, "a")
        orch.update_job(self.con, "a", queue="q2")
        queues = {r["id"]: r["queue"] for r in orch.list_runs(self.con)}
        self.assertEqual(queues, {done: "default", pending: "q2"})  # history stays where it ran


class TestJobCli(DBTestCase):
    def test_create_query_change_enable_disable_delete(self):
        self.cli("create", "backup", "--type", "command", "--command", "echo งานไทย",
                 "--cron", "0 2 * * *", "--every", "10m", "--priority", "8", "--retry", "2",
                 "--desc", "ทดสอบ")
        self.cli("create", "notify", "--type", "http", "--url", "http://localhost/x",
                 "--header", "X-A: 1", "--expect", "200,201", "--after", "backup", "--after-on", "always")
        out = self.cli("query").stdout
        self.assertIn("backup", out)
        self.assertIn("cron: 0 2 * * *", out)
        jobs = json.loads(self.cli("query", "--json").stdout)
        self.assertEqual([j["name"] for j in jobs], ["backup", "notify"])
        self.assertEqual(jobs[0]["params"]["command"], "echo งานไทย")
        self.assertEqual(jobs[0]["priority"], 8)
        self.assertEqual(jobs[1]["params"]["headers"], {"X-A": "1"})
        self.assertEqual(jobs[1]["after_job"], "backup")

        self.cli("change", "backup", "--priority", "2", "--at", "2099-01-01 00:00")
        j = json.loads(self.cli("query", "backup", "--json").stdout)[0]
        self.assertEqual(j["priority"], 2)
        self.assertEqual(j["max_retry"], 2)
        self.assertEqual(j["params"]["command"], "echo งานไทย")
        self.assertEqual([(s["kind"], s["expr"]) for s in j["schedules"]], [("at", "2099-01-01 00:00:00")])

        self.cli("change", "notify", "--after", "")
        self.assertIsNone(json.loads(self.cli("query", "notify", "--json").stdout)[0]["after_job"])

        self.cli("disable", "backup")
        self.assertEqual(json.loads(self.cli("query", "backup", "--json").stdout)[0]["enabled"], 0)
        self.cli("enable", "backup")
        self.cli("delete", "notify")
        self.assertEqual(len(json.loads(self.cli("query", "--json").stdout)), 1)

    def test_cli_errors(self):
        r = self.cli("create", "x", "--type", "command", check=False)
        self.assertEqual(r.returncode, 2)
        self.assertIn("command", r.stderr)
        r = self.cli("query", "nope", check=False)
        self.assertEqual(r.returncode, 2)
        self.assertIn("not found", r.stderr)
        r = self.cli("create", "x", "--type", "command", "--params", "{bad", check=False)
        self.assertIn("JSON", r.stderr)

    def test_change_type_requires_params(self):
        self.cli("create", "x", "--type", "command", "--command", "echo 1")
        r = self.cli("change", "x", "--type", "python", check=False)
        self.assertEqual(r.returncode, 2)
        self.cli("change", "x", "--type", "python", "--file", "a.py")
        j = json.loads(self.cli("query", "x", "--json").stdout)[0]
        self.assertEqual((j["job_type"], j["params"]), ("python", {"file": "a.py"}))


if __name__ == "__main__":
    unittest.main()
