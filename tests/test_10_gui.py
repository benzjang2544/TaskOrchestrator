"""Step 10: GUI (driven directly, no mainloop). Skipped when Tk can't open a display."""
import unittest

from helpers import DBTestCase, orch

try:
    import tkinter as tk
    _root = tk.Tk()
    _root.destroy()
    HAVE_TK = True
except Exception:  # no tkinter / no display
    HAVE_TK = False

if HAVE_TK:
    import orch_gui

CMD = {"command": "echo hi"}


@unittest.skipUnless(HAVE_TK, "tkinter display not available")
class TestGui(DBTestCase):
    def setUp(self):
        super().setUp()
        self.root = tk.Tk()
        self.root.withdraw()
        self.errors = []
        self.answer = True
        self.app = orch_gui.App(self.root, auto_refresh=False)
        self.app.error = self.errors.append
        self.app.confirm = lambda msg: self.answer
        self.app.con.close()
        self.app.con = orch.connect()

    def tearDown(self):
        self.app.con.close()
        self.root.destroy()
        super().tearDown()

    def jobs(self):
        t = self.app.jobs_tree
        return {t.set(i, "name"): {c: t.set(i, c) for c in t["columns"]} for i in t.get_children()}

    def select_job(self, name):
        t = self.app.jobs_tree
        iid = next(i for i in t.get_children() if t.set(i, "name") == name)
        t.selection_set(iid)
        return iid

    def test_jobs_table_and_in_place_refresh(self):
        orch.create_job(self.con, "a", "command", CMD, ["every: 10m"], priority=3)
        orch.create_job(self.con, "b", "command", CMD, after_job="a", enabled=0)
        self.app.refresh()
        j = self.jobs()
        self.assertEqual((j["a"]["prio"], j["a"]["schedule"], j["a"]["enabled"]), ("3", "every: 10m", "yes"))
        self.assertEqual((j["b"]["after"], j["b"]["enabled"]), ("a (success)", "no"))
        self.assertTrue(j["a"]["next"])
        iid = self.select_job("a")
        orch.update_job(self.con, "a", priority=8)
        self.app.refresh()
        self.assertEqual(self.app.jobs_tree.set(iid, "prio"), "8")  # same item updated in place
        self.assertEqual(self.app.jobs_tree.selection(), (iid,))  # selection kept
        orch.delete_job(self.con, "b")
        self.app.refresh()
        self.assertEqual(list(self.jobs()), ["a"])

    def test_job_buttons(self):
        orch.create_job(self.con, "a", "command", CMD)
        self.app.refresh()
        self.app.run_now()
        self.assertEqual(self.errors, ["Select a job first"])
        self.select_job("a")
        self.app.run_now()
        self.assertEqual([r["trigger"] for r in orch.list_runs(self.con)], ["manual"])
        self.app.toggle_job()
        self.assertEqual(orch.get_job(self.con, "a")["enabled"], 0)
        self.app.toggle_job()
        self.assertEqual(orch.get_job(self.con, "a")["enabled"], 1)
        self.answer = False
        self.app.delete_job()
        self.assertEqual(len(orch.list_jobs(self.con)), 1)
        self.answer = True
        self.app.delete_job()
        self.assertEqual(orch.list_jobs(self.con), [])

    def test_errors_from_orch_are_shown(self):
        orch.create_job(self.con, "a", "command", CMD)
        orch.create_job(self.con, "b", "command", CMD, after_job="a")
        self.app.refresh()
        self.select_job("a")
        self.app.delete_job()
        self.assertIn("referenced", self.errors[-1])

    def test_runs_tab_colors_filter_cancel_pause(self):
        orch.create_job(self.con, "a", "command", CMD, dedup_policy="allow")
        r1, _ = orch.enqueue(self.con, "a")
        r2, _ = orch.enqueue(self.con, "a")
        orch.cancel_run(self.con, r1)
        self.app.refresh()
        t = self.app.runs_tree
        self.assertEqual(t.get_children(), (str(r2), str(r1)))
        self.assertEqual(t.item(str(r1), "tags"), ("Cancelled",))
        self.assertEqual(t.set(str(r2), "status"), "Pending")
        self.app.status_filter.set("Pending")
        self.app.refresh()
        self.assertEqual(t.get_children(), (str(r2),))
        t.selection_set(str(r2))
        self.app.cancel_run()
        self.assertEqual(orch.get_run(self.con, r2)["status"], "Cancelled")
        self.app.status_filter.set("")
        self.app.refresh()
        t.selection_set(str(r2))
        self.app.set_paused(True)  # queue taken from the selected run
        self.assertTrue(orch.is_paused(self.con, "default"))
        self.assertIn("(paused)", self.app.status_var.get())
        self.app.queue_filter.set("default")
        self.app.set_paused(False)
        self.assertFalse(orch.is_paused(self.con, "default"))

    def test_status_bar_engine_state(self):
        orch.create_job(self.con, "a", "command", CMD, queue="q1")
        self.app.refresh()
        self.assertIn("q1: ○ stopped", self.app.status_var.get())
        self.con.execute("INSERT INTO engine_lock(queue, host, pid, heartbeat) VALUES('q1', 'h', 1, ?)",
                         (orch.now_str(),))
        self.app.refresh()
        self.assertIn("q1: ● running", self.app.status_var.get())

    def test_stop_engine_button(self):
        orch.create_job(self.con, "a", "command", CMD, queue="q1")
        self.app.queue_filter.set("q1")
        self.app.stop_engine()
        self.assertIn("no running engine", self.errors[-1])
        orch.acquire_lock(self.con, "q1", "h", 1)
        self.answer = False
        self.app.stop_engine()  # declined: nothing requested
        self.assertFalse(orch.engine_stop_requested(self.con, "q1", "h", 1))
        self.answer = True
        self.app.stop_engine()
        self.assertTrue(orch.engine_stop_requested(self.con, "q1", "h", 1))
        self.assertIn("q1: ● running (stopping)", self.app.status_var.get())

    def test_log_view_appends_while_running(self):
        orch.create_job(self.con, "a", "command", CMD)
        rid, _ = orch.enqueue(self.con, "a")
        orch.claim_next(self.con, "default")
        orch.add_log(self.con, rid, "OUT", "line 1")
        self.app.refresh()
        self.app.runs_tree.selection_set(str(rid))
        self.app.open_log()
        self.assertIn("line 1", self.app.log_content())
        self.assertIn("[Running]", self.app.log_title.get())
        orch.add_log(self.con, rid, "OUT", "สอง")
        self.app.refresh()
        content = self.app.log_content()
        self.assertIn("สอง", content)
        self.assertEqual(content.count("line 1"), 1)  # appended, not reloaded
        orch.finalize(self.con, rid, "exited", 0)
        self.app.refresh()
        self.assertIn("finished: Succeeded", self.app.log_content())
        self.assertIn("[Succeeded]", self.app.log_title.get())

    def test_new_job_dialog_templates_and_save(self):
        d = self.app.new_job()
        self.assertIn('"command"', d.params_text.get("1.0", "end"))
        d.vars["job_type"].set("python")
        d.on_type_change()  # untouched -> python template
        self.assertIn('"file"', d.params_text.get("1.0", "end"))
        d.params_text.delete("1.0", "end")
        d.params_text.insert("1.0", '{"command": "echo edited"}')
        d.vars["job_type"].set("command")
        d.on_type_change()  # edited -> kept
        self.assertIn("echo edited", d.params_text.get("1.0", "end"))
        d.vars["name"].set("nj")
        d.vars["priority"].set("7")
        d.sched_text.insert("1.0", "cron: */5 * * * *\n\nevery: 10m\nstartup")
        self.assertTrue(d.save())
        j = orch.get_job(self.con, "nj")
        self.assertEqual((j["priority"], j["params"]), (7, {"command": "echo edited"}))
        self.assertEqual([s["kind"] for s in j["schedules"]], ["cron", "every", "startup"])
        self.assertIn("nj", self.jobs())  # table refreshed

    def test_dialog_validation_errors(self):
        d = self.app.new_job()
        d.vars["name"].set("x")
        d.vars["priority"].set("high")
        self.assertFalse(d.save())
        d.vars["priority"].set("5")
        d.params_text.delete("1.0", "end")
        d.params_text.insert("1.0", "{bad json")
        self.assertFalse(d.save())
        d.params_text.delete("1.0", "end")
        d.params_text.insert("1.0", "{}")
        self.assertFalse(d.save())  # missing command -> OrchError from orch.create_job
        d.params_text.delete("1.0", "end")
        d.params_text.insert("1.0", '{"command": "echo"}')
        d.sched_text.insert("1.0", "at: 2000-01-01 00:00")
        self.assertFalse(d.save())
        self.assertEqual(len(self.errors), 4)
        self.assertEqual(orch.list_jobs(self.con), [])  # nothing half-created

    def test_edit_dialog_keeps_schedule_state_when_untouched(self):
        orch.create_job(self.con, "a", "command", CMD, ["every: 10m"])
        self.con.execute("UPDATE schedule SET next_run_at='2099-01-01 00:00:00'")
        self.app.refresh()
        self.select_job("a")
        d = self.app.edit_job()
        self.assertEqual(str(d.name_entry.cget("state")), "readonly")
        d.vars["priority"].set("9")
        self.assertTrue(d.save())
        j = orch.get_job(self.con, "a")
        self.assertEqual(j["priority"], 9)
        self.assertEqual(j["schedules"][0]["next_run_at"], "2099-01-01 00:00:00")
        self.select_job("a")
        d = self.app.edit_job()
        d.sched_text.delete("1.0", "end")
        d.sched_text.insert("1.0", "every: 1h")
        self.assertTrue(d.save())
        self.assertEqual(orch.get_job(self.con, "a")["schedules"][0]["expr"], "1h")

    def test_edit_dialog_reenable_and_clear_description(self):
        orch.create_job(self.con, "a", "command", CMD, ["every: 10m"], enabled=0, description="old")
        self.con.execute("UPDATE schedule SET next_run_at='2000-01-01 00:00:00'")
        self.app.refresh()
        self.select_job("a")
        d = self.app.edit_job()
        d.enabled_var.set(True)
        d.vars["description"].set("")
        self.assertTrue(d.save())
        j = orch.get_job(self.con, "a")
        self.assertEqual((j["enabled"], j["description"]), (1, None))
        self.assertGreater(j["schedules"][0]["next_run_at"], orch.now_str())  # no missed fire on re-enable

    def test_database_errors_are_shown(self):
        def busy(*a):
            raise orch.sqlite3.OperationalError("database is locked")
        self.assertEqual(self.app.call(busy), (False, None))
        self.assertIn("database is locked", self.errors[-1])


if __name__ == "__main__":
    unittest.main()
