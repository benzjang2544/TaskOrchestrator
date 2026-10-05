import threading
import unittest

from helpers import DBTestCase, orch


class TestFoundation(DBTestCase):
    def test_schema_tables_exist(self):
        names = {r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("job_def", "schedule", "job_run", "run_log", "queue_state", "engine_lock"):
            self.assertIn(t, names)

    def test_wal_and_autocommit(self):
        self.assertEqual(self.con.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertIsNone(self.con.isolation_level)

    def test_tx_commits_and_rolls_back(self):
        with orch.tx(self.con):
            self.con.execute("INSERT INTO queue_state(name, paused) VALUES('a', 0)")
        with self.assertRaises(RuntimeError):
            with orch.tx(self.con):
                self.con.execute("INSERT INTO queue_state(name, paused) VALUES('b', 0)")
                raise RuntimeError("boom")
        names = [r[0] for r in self.con.execute("SELECT name FROM queue_state")]
        self.assertEqual(names, ["a"])
        self.assertFalse(self.con.in_transaction)

    def test_connection_per_thread(self):
        errors = []

        def worker(i):
            try:
                c = orch.connect()
                for j in range(20):
                    with orch.tx(c):
                        c.execute("INSERT INTO run_log(run_id, ts, level, message) VALUES(?,?,?,?)",
                                  (i, orch.now_str(), "INFO", str(j)))
                c.close()
            except Exception as e:  # pragma: no cover
                errors.append(e)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM run_log").fetchone()[0], 80)

    def test_long_lived_connection_sees_other_writers(self):
        # fetchone() must not pin the connection to an old WAL snapshot
        self.assertIsNone(self.con.execute("SELECT * FROM queue_state WHERE name='x'").fetchone())
        self.con.execute("SELECT COUNT(*) FROM queue_state").fetchone()
        self.cli("pause", "--queue", "x")
        self.assertTrue(orch.is_paused(self.con, "x"))
        self.assertFalse(self.con.in_transaction)

    def test_time_helpers(self):
        self.assertRegex(orch.now_str(), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(orch.fmt_ts(orch.parse_ts("2026-10-01 08:00")), "2026-10-01 08:00:00")
        self.assertEqual(orch.add_seconds("2026-12-31 23:59:30", 45), "2027-01-01 00:00:15")
        self.assertLess("2026-09-30 23:59:59", "2026-10-01 00:00:00")  # string comparable
        for bad in ("2026-13-01 00:00", "yesterday", "2026-02-30 00:00"):
            with self.assertRaises(orch.OrchError):
                orch.parse_ts(bad)

    def test_cli_init(self):
        r = self.cli("init")
        self.assertIn("initialized", r.stdout)


if __name__ == "__main__":
    unittest.main()
