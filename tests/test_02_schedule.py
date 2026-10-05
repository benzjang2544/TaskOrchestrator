import datetime as dt
import unittest

from helpers import orch

D = dt.datetime


def nxt(expr, after):
    return orch.CronExpr(expr).next_after(after)


class TestCron(unittest.TestCase):
    def test_every_five_minutes(self):
        self.assertEqual(nxt("*/5 * * * *", D(2026, 1, 1, 10, 3)), D(2026, 1, 1, 10, 5))
        self.assertEqual(nxt("*/5 * * * *", D(2026, 1, 1, 10, 5)), D(2026, 1, 1, 10, 10))
        self.assertEqual(nxt("*/5 * * * *", D(2026, 1, 1, 10, 57, 30)), D(2026, 1, 1, 11, 0))

    def test_strictly_after_ignores_seconds(self):
        self.assertEqual(nxt("* * * * *", D(2026, 1, 1, 10, 3, 59)), D(2026, 1, 1, 10, 4))

    def test_across_month_and_year(self):
        self.assertEqual(nxt("0 0 1 * *", D(2026, 1, 31, 12, 0)), D(2026, 2, 1))
        self.assertEqual(nxt("30 23 31 12 *", D(2026, 12, 31, 23, 30)), D(2027, 12, 31, 23, 30))
        self.assertEqual(nxt("0 0 * * *", D(2026, 12, 31, 23, 59)), D(2027, 1, 1))
        # dom 31 skips months without a 31st
        self.assertEqual(nxt("0 12 31 * *", D(2026, 4, 1)), D(2026, 5, 31, 12))

    def test_dom_dow_or(self):
        # 2026-10-01 is a Thursday. "on the 15th OR on Mondays"
        self.assertEqual(nxt("0 9 15 * 1", D(2026, 10, 1)), D(2026, 10, 5, 9))  # Monday
        self.assertEqual(nxt("0 9 15 * 1", D(2026, 10, 13)), D(2026, 10, 15, 9))  # 15th (Thu)
        # dow only (dom='*') -> only dow matters
        self.assertEqual(nxt("0 9 * * 1", D(2026, 10, 1)), D(2026, 10, 5, 9))
        # dom only (dow='*') -> only dom matters
        self.assertEqual(nxt("0 9 15 * *", D(2026, 10, 1)), D(2026, 10, 15, 9))

    def test_sunday_zero_and_seven(self):
        # 2026-10-04 is a Sunday
        self.assertEqual(nxt("0 0 * * 0", D(2026, 10, 1)), D(2026, 10, 4))
        self.assertEqual(nxt("0 0 * * 7", D(2026, 10, 1)), D(2026, 10, 4))

    def test_feb_29(self):
        self.assertEqual(nxt("0 0 29 2 *", D(2026, 1, 1)), D(2028, 2, 29))
        self.assertEqual(nxt("0 0 29 2 *", D(2028, 2, 29)), D(2032, 2, 29))

    def test_lists_ranges_steps(self):
        c = orch.CronExpr("5/10 1-3,8 * * 1-5")
        self.assertEqual(sorted(c.minutes), [5, 15, 25, 35, 45, 55])
        self.assertEqual(sorted(c.hours), [1, 2, 3, 8])
        self.assertEqual(sorted(c.dows), [1, 2, 3, 4, 5])
        self.assertEqual(sorted(orch.CronExpr("0-10/5 * * * *").minutes), [0, 5, 10])

    def test_impossible_raises(self):
        with self.assertRaises(orch.OrchError):
            nxt("0 0 31 2 *", D(2026, 1, 1))  # Feb 31 never exists

    def test_invalid_expressions(self):
        for bad in ("", "* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *",
                    "* * * * 8", "*/0 * * * *", "a * * * *", "5-1 * * * *", "*-5 * * * *"):
            with self.assertRaises(orch.OrchError, msg=bad):
                orch.CronExpr(bad)


class TestEveryAtStartup(unittest.TestCase):
    def test_parse_every(self):
        self.assertEqual(orch.parse_every("30s"), 30)
        self.assertEqual(orch.parse_every("5m"), 300)
        self.assertEqual(orch.parse_every("2h"), 7200)
        self.assertEqual(orch.parse_every("1d"), 86400)
        self.assertEqual(orch.parse_every("90"), 90)
        self.assertEqual(orch.parse_every(45), 45)
        for bad in ("0", "0m", "-5", "5x", "", "m"):
            with self.assertRaises(orch.OrchError, msg=bad):
                orch.parse_every(bad)

    def test_every_anchors_on_previous(self):
        prev = D(2026, 1, 1, 10, 0, 7)
        self.assertEqual(orch.next_fire("every", "10m", prev), D(2026, 1, 1, 10, 10, 7))

    def test_at(self):
        now = D(2026, 1, 1)
        self.assertEqual(orch.validate_schedule("at", "2026-10-01 08:00", now), "2026-10-01 08:00:00")
        with self.assertRaises(orch.OrchError):
            orch.validate_schedule("at", "2025-10-01 08:00", now)
        self.assertEqual(orch.initial_next_run("at", "2026-10-01 08:00:00", now), "2026-10-01 08:00:00")
        self.assertIsNone(orch.next_fire("at", "2026-10-01 08:00:00", D(2026, 10, 1, 8)))

    def test_initial_next_run(self):
        now = D(2026, 1, 1, 10, 3, 20)
        self.assertEqual(orch.initial_next_run("cron", "*/5 * * * *", now), "2026-01-01 10:05:00")
        self.assertEqual(orch.initial_next_run("every", "30s", now), "2026-01-01 10:03:50")
        self.assertIsNone(orch.initial_next_run("startup", None, now))

    def test_schedule_lines(self):
        self.assertEqual(orch.parse_schedule_line("cron: */5 * * * *"), ("cron", "*/5 * * * *"))
        self.assertEqual(orch.parse_schedule_line("every: 10m"), ("every", "10m"))
        self.assertEqual(orch.parse_schedule_line("at: 2026-10-01 08:00"), ("at", "2026-10-01 08:00"))
        self.assertEqual(orch.parse_schedule_line("startup"), ("startup", None))
        with self.assertRaises(orch.OrchError):
            orch.parse_schedule_line("hourly")
        with self.assertRaises(orch.OrchError):
            orch.validate_schedule("weekly", "x")


if __name__ == "__main__":
    unittest.main()
