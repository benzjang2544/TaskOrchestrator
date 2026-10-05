#!/usr/bin/env python3
"""Orch GUI (tkinter). Reads/writes the same DB as orch.py and only calls orch.py functions.

Usage: python orch_gui.py [--db PATH]   (default: $ORCH_DB or orch.db next to orch.py)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tkinter as tk
from tkinter import messagebox, ttk

import orch

REFRESH_MS = 2000
RUNS_LIMIT = 200
STATUS_COLORS = {
    "Pending": "#fff4c2", "Running": "#cfe8ff", "Succeeded": "#d7f2d7", "Failed": "#ffe0b8",
    "Dead": "#f6c6c6", "Cancelled": "#e2e2e2", "Skipped": "#efefef",
}
JOB_COLUMNS = (("name", "Name", 160), ("type", "Type", 80), ("queue", "Queue", 80), ("prio", "Prio", 45),
               ("schedule", "Schedule", 220), ("next", "Next run", 140), ("enabled", "Enabled", 60),
               ("after", "After", 140))
RUN_COLUMNS = (("id", "ID", 55), ("job", "Job", 140), ("queue", "Queue", 80), ("status", "Status", 80),
               ("prio", "Prio", 40), ("trigger", "Trigger", 110), ("try", "Try", 35),
               ("scheduled", "Scheduled", 135), ("started", "Started", 135), ("finished", "Finished", 135),
               ("exit", "Exit", 40), ("error", "Error", 260))


def _s(v) -> str:
    return "" if v is None else str(v)


def sync_tree(tree: ttk.Treeview, rows: list[tuple[str, list, tuple]]) -> None:
    """Make `tree` show `rows` [(iid, values, tags)] by updating items in place.

    Existing items keep their identity, so selection and scroll position survive a refresh.
    """
    existing = set(tree.get_children(""))
    wanted = set()
    for index, (iid, values, tags) in enumerate(rows):
        values = [_s(v) for v in values]
        wanted.add(iid)
        if iid in existing:
            if [_s(v) for v in tree.item(iid, "values")] != values:
                tree.item(iid, values=values)
            if tuple(tree.item(iid, "tags")) != tuple(tags):
                tree.item(iid, tags=tags)
            if tree.index(iid) != index:
                tree.move(iid, "", index)
        else:
            tree.insert("", index, iid=iid, values=values, tags=tags)
    for iid in existing - wanted:
        tree.delete(iid)


def _make_tree(parent, columns) -> ttk.Treeview:
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", selectmode="browse")
    for key, title, width in columns:
        tree.heading(key, text=title)
        tree.column(key, width=width, stretch=key in ("schedule", "error"))
    ys = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    xs = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
    tree.grid(row=0, column=0, sticky="nsew")
    ys.grid(row=0, column=1, sticky="ns")
    xs.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return tree


class App:
    def __init__(self, root: tk.Tk, auto_refresh: bool = True):
        self.root = root
        self.con = orch.connect()
        self.auto_refresh = auto_refresh
        self.log_run_id: int | None = None
        self.log_last_id = 0
        root.title(f"Orch — {orch.db_path()}")
        root.geometry("1150x620")

        self.status_var = tk.StringVar()
        ttk.Label(root, textvariable=self.status_var, anchor="w", padding=(8, 4)).pack(fill="x")

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self._build_jobs_tab()
        self._build_runs_tab()
        self._build_log_tab()
        if auto_refresh:
            self._tick()
        else:
            self.refresh()

    # ----- UI hooks (overridden in tests) -----
    def error(self, msg: str) -> None:
        messagebox.showerror("Orch", msg, parent=self.root)

    def confirm(self, msg: str) -> bool:
        return messagebox.askyesno("Orch", msg, parent=self.root)

    def call(self, fn, *args, **kw):
        """Run an orch.py function; show OrchError to the user. Returns (ok, result)."""
        try:
            return True, fn(*args, **kw)
        except orch.OrchError as e:
            self.error(str(e))
            return False, None

    # ----- Jobs tab -----
    def _build_jobs_tab(self):
        tab = ttk.Frame(self.nb, padding=4)
        self.nb.add(tab, text="Jobs")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 4))
        for text, cmd in (("New", self.new_job), ("Edit", self.edit_job), ("Run now", self.run_now),
                          ("Enable/Disable", self.toggle_job), ("Delete", self.delete_job)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)
        self.jobs_tree = _make_tree(tab, JOB_COLUMNS)
        self.jobs_tree.tag_configure("disabled", foreground="#888888")
        self.jobs_tree.bind("<Double-1>", lambda e: self.edit_job())

    def selected_job(self) -> str | None:
        sel = self.jobs_tree.selection()
        if not sel:
            self.error("Select a job first")
            return None
        return self.jobs_tree.set(sel[0], "name")

    def new_job(self):
        return JobDialog(self, None)

    def edit_job(self):
        name = self.selected_job()
        if name:
            ok, job = self.call(orch.get_job, self.con, name)
            if ok:
                return JobDialog(self, job)

    def run_now(self):
        name = self.selected_job()
        if name and self.call(orch.enqueue, self.con, name, "manual")[0]:
            self.refresh()

    def toggle_job(self):
        name = self.selected_job()
        if name:
            ok, job = self.call(orch.get_job, self.con, name)
            if ok and self.call(orch.set_enabled, self.con, name, not job["enabled"])[0]:
                self.refresh()

    def delete_job(self):
        name = self.selected_job()
        if name and self.confirm(f"Delete job {name!r}? (run history is kept)"):
            if self.call(orch.delete_job, self.con, name)[0]:
                self.refresh()

    # ----- Queue/Runs tab -----
    def _build_runs_tab(self):
        tab = ttk.Frame(self.nb, padding=4)
        self.nb.add(tab, text="Queue / Runs")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="Status:").pack(side="left")
        self.status_filter = ttk.Combobox(bar, values=("",) + orch.RUN_STATUSES, width=11, state="readonly")
        self.status_filter.pack(side="left", padx=(2, 8))
        ttk.Label(bar, text="Queue:").pack(side="left")
        self.queue_filter = ttk.Combobox(bar, values=("",), width=14)
        self.queue_filter.pack(side="left", padx=(2, 8))
        for w in (self.status_filter, self.queue_filter):
            w.bind("<<ComboboxSelected>>", lambda e: self.refresh())
            w.bind("<Return>", lambda e: self.refresh())
        for text, cmd in (("Cancel run", self.cancel_run), ("Pause queue", lambda: self.set_paused(True)),
                          ("Resume queue", lambda: self.set_paused(False)), ("Open log", self.open_log)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)
        self.runs_tree = _make_tree(tab, RUN_COLUMNS)
        for status, color in STATUS_COLORS.items():
            self.runs_tree.tag_configure(status, background=color)
        self.runs_tree.bind("<Double-1>", lambda e: self.open_log())

    def selected_run(self, quiet=False) -> int | None:
        sel = self.runs_tree.selection()
        if not sel:
            if not quiet:
                self.error("Select a run first")
            return None
        return int(sel[0])

    def cancel_run(self):
        rid = self.selected_run()
        if rid is not None and self.call(orch.cancel_run, self.con, rid)[0]:
            self.refresh()

    def target_queue(self) -> str | None:
        q = self.queue_filter.get().strip()
        if q:
            return q
        rid = self.selected_run(quiet=True)
        if rid is not None:
            return self.runs_tree.set(str(rid), "queue")
        self.error("Choose a queue in the Queue filter (or select a run)")
        return None

    def set_paused(self, paused: bool):
        q = self.target_queue()
        if q and self.call(orch.set_paused, self.con, q, paused)[0]:
            self.refresh()

    def open_log(self):
        rid = self.selected_run()
        if rid is not None:
            self.show_log(rid)
            self.nb.select(self.log_tab)

    # ----- Log tab -----
    def _build_log_tab(self):
        self.log_tab = ttk.Frame(self.nb, padding=4)
        self.nb.add(self.log_tab, text="Log")
        self.log_title = tk.StringVar(value="Double-click a run to see its log")
        ttk.Label(self.log_tab, textvariable=self.log_title, anchor="w").pack(fill="x", pady=(0, 4))
        frame = ttk.Frame(self.log_tab)
        frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(frame, wrap="none", font=("Consolas", 10), state="disabled")
        ys = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=ys.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        self.log_text.tag_configure("WARN", foreground="#b35c00")
        self.log_text.tag_configure("ERROR", foreground="#c00000")

    def show_log(self, run_id: int):
        self.log_run_id, self.log_last_id = run_id, 0
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._update_log(force=True)

    def _update_log(self, force=False):
        if self.log_run_id is None:
            return
        ok, run = self.call(orch.get_run, self.con, self.log_run_id)
        if not ok:
            self.log_run_id = None
            return
        self.log_title.set(f"Run #{run['id']}  {run['job_name']}  [{run['status']}]  attempt {run['attempt']}"
                           f"  trigger {run['trigger']}" + (f"  — {run['error']}" if run["error"] else ""))
        if not force and run["status"] not in ("Pending", "Running") and self._log_final_loaded:
            return  # finished and fully loaded: nothing more to fetch
        lines = orch.get_logs(self.con, self.log_run_id, self.log_last_id)
        self._log_final_loaded = run["status"] not in ("Pending", "Running")
        if not lines:
            return
        at_end = self.log_text.yview()[1] >= 0.999
        self.log_text.configure(state="normal")
        for ln in lines:
            self.log_text.insert("end", f"{ln['ts']} {ln['level']:<5} {ln['message']}\n", (ln["level"],))
            self.log_last_id = ln["id"]
        self.log_text.configure(state="disabled")
        if at_end:
            self.log_text.see("end")

    def log_content(self) -> str:
        return self.log_text.get("1.0", "end-1c")

    # ----- refresh -----
    def refresh(self):
        try:
            self._refresh_status()
            self._refresh_jobs()
            self._refresh_runs()
            self._update_log()
        except Exception as e:  # keep the GUI alive if the DB is briefly unavailable
            self.status_var.set(f"refresh failed: {e}")

    def _tick(self):
        self.refresh()
        self.root.after(REFRESH_MS, self._tick)

    def _refresh_status(self):
        states = orch.engine_states(self.con)
        parts = []
        for s in states:
            txt = f"{s['queue']}: {'● running' if s['running'] else '○ stopped'}"
            if s["paused"]:
                txt += " (paused)"
            parts.append(txt)
        self.status_var.set("Engines —  " + ("   |   ".join(parts) if parts else "no queues yet"))
        self.queue_filter.configure(values=("",) + tuple(s["queue"] for s in states))

    def _refresh_jobs(self):
        rows = []
        for j in orch.list_jobs(self.con):
            after = f"{j['after_job']} ({j['after_on']})" if j["after_job"] else ""
            rows.append((f"job-{j['id']}", [j["name"], j["job_type"], j["queue"], j["priority"],
                                             orch.schedule_summary(j), j["next_run_at"],
                                             "yes" if j["enabled"] else "no", after],
                         () if j["enabled"] else ("disabled",)))
        sync_tree(self.jobs_tree, rows)

    def _refresh_runs(self):
        runs = orch.list_runs(self.con, queue=self.queue_filter.get().strip() or None,
                              status=self.status_filter.get() or None, limit=RUNS_LIMIT)
        sync_tree(self.runs_tree, [
            (str(r["id"]), [r["id"], r["job_name"], r["queue"], r["status"], r["priority"], r["trigger"],
                            r["attempt"], r["scheduled_at"], r["started_at"], r["finished_at"], r["exit_code"],
                            (r["error"] or "").replace("\n", " ")], (r["status"],)) for r in runs])

    _log_final_loaded = False


class JobDialog:
    """Create/edit a job. Params are JSON (template per type); schedules one per line."""

    INT_FIELDS = (("priority", "Priority"), ("timeout_sec", "Timeout (s, 0=none)"), ("max_retry", "Max retry"),
                  ("retry_delay_sec", "Retry delay (s)"))

    def __init__(self, app: App, job: dict | None):
        self.app, self.job, self.result = app, job, None
        self.win = tk.Toplevel(app.root)
        self.win.title(f"Edit job {job['name']}" if job else "New job")
        self.win.transient(app.root)
        d = job or dict(orch.JOB_DEFAULTS, name="", job_type="command", params=None, schedules=[])
        self.vars: dict[str, tk.Variable] = {}
        form = ttk.Frame(self.win, padding=8)
        form.pack(fill="both", expand=True)

        def row(r, label, widget):
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", pady=2)
            widget.grid(row=r, column=1, sticky="ew", pady=2)
            return widget

        def var(key, value):
            v = tk.StringVar(value=_s(value))
            self.vars[key] = v
            return v

        r = 0
        self.name_entry = row(r, "Name", ttk.Entry(form, textvariable=var("name", d["name"]),
                                                   state="readonly" if job else "normal"))
        r += 1
        self.type_box = row(r, "Type", ttk.Combobox(form, textvariable=var("job_type", d["job_type"]),
                                                    values=orch.JOB_TYPES, state="readonly"))
        self.type_box.bind("<<ComboboxSelected>>", lambda e: self.on_type_change())
        r += 1
        row(r, "Queue", ttk.Entry(form, textvariable=var("queue", d["queue"])))
        for key, label in self.INT_FIELDS:
            r += 1
            row(r, label, ttk.Entry(form, textvariable=var(key, d[key])))
        r += 1
        row(r, "Retry backoff", ttk.Entry(form, textvariable=var("retry_backoff", d["retry_backoff"])))
        for key, label, values in (("dedup_policy", "Dedup", orch.DEDUP_POLICIES),
                                   ("missed_policy", "Missed", orch.MISSED_POLICIES)):
            r += 1
            row(r, label, ttk.Combobox(form, textvariable=var(key, d[key]), values=values, state="readonly"))
        r += 1
        others = [""] + [j["name"] for j in orch.list_jobs(app.con) if not job or j["name"] != job["name"]]
        row(r, "After job", ttk.Combobox(form, textvariable=var("after_job", d["after_job"]), values=others,
                                         state="readonly"))
        r += 1
        row(r, "After on", ttk.Combobox(form, textvariable=var("after_on", d["after_on"]), values=orch.AFTER_ON,
                                        state="readonly"))
        r += 1
        row(r, "Description", ttk.Entry(form, textvariable=var("description", d["description"])))
        r += 1
        self.enabled_var = tk.BooleanVar(value=bool(d["enabled"]))
        row(r, "Enabled", ttk.Checkbutton(form, variable=self.enabled_var))

        r += 1
        ttk.Label(form, text="Params (JSON)").grid(row=r, column=0, sticky="nw", pady=2)
        self.params_text = tk.Text(form, height=8, width=60, font=("Consolas", 10))
        self.params_text.grid(row=r, column=1, sticky="nsew", pady=2)
        self._template = ""
        if job:
            self.params_text.insert("1.0", json.dumps(job["params"], ensure_ascii=False, indent=2))
        else:
            self._set_template(d["job_type"])

        r += 1
        ttk.Label(form, text="Schedules\n(one per line)").grid(row=r, column=0, sticky="nw", pady=2)
        self.sched_text = tk.Text(form, height=4, width=60, font=("Consolas", 10))
        self.sched_text.grid(row=r, column=1, sticky="nsew", pady=2)
        self._orig_sched = [orch.format_schedule(s["kind"], s["expr"]) for s in d["schedules"]]
        self.sched_text.insert("1.0", "\n".join(self._orig_sched))
        r += 1
        ttk.Label(form, text="e.g.  cron: */5 * * * *   every: 10m   at: 2026-10-01 08:00   startup",
                  foreground="#666666").grid(row=r, column=1, sticky="w")

        r += 1
        btns = ttk.Frame(form)
        btns.grid(row=r, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(btns, text="Save", command=self.save).pack(side="left", padx=2)
        ttk.Button(btns, text="Cancel", command=self.win.destroy).pack(side="left", padx=2)
        form.columnconfigure(1, weight=1)
        form.rowconfigure(r - 3, weight=1)

    def _set_template(self, job_type: str):
        tpl = {k: v for k, v in orch.PARAM_TEMPLATES[job_type].items() if v not in (None, {}, "")}
        self._template = json.dumps(tpl, ensure_ascii=False, indent=2)
        self.params_text.delete("1.0", "end")
        self.params_text.insert("1.0", self._template)

    def on_type_change(self):
        """Swap in the new type's template only if the user hasn't edited the params."""
        current = self.params_text.get("1.0", "end").strip()
        if not self.job and current == self._template.strip():
            self._set_template(self.vars["job_type"].get())

    def save(self):
        v = {k: x.get().strip() for k, x in self.vars.items()}
        try:
            fields = {k: int(v[k]) for k, _ in self.INT_FIELDS}
            fields["retry_backoff"] = float(v["retry_backoff"])
        except ValueError:
            self.app.error("Priority, timeout, retry, delay must be integers and backoff a number")
            return False
        try:
            params = json.loads(self.params_text.get("1.0", "end").strip() or "{}")
        except json.JSONDecodeError as e:
            self.app.error(f"Params is not valid JSON: {e}")
            return False
        fields.update(queue=v["queue"], dedup_policy=v["dedup_policy"], missed_policy=v["missed_policy"],
                      after_job=v["after_job"], after_on=v["after_on"], description=v["description"] or None,
                      enabled=1 if self.enabled_var.get() else 0)
        lines = [ln.strip() for ln in self.sched_text.get("1.0", "end").splitlines() if ln.strip()]
        if self.job:
            schedules = None if lines == self._orig_sched else lines  # untouched -> keep next_run_at
            ok, res = self.app.call(orch.update_job, self.app.con, self.job["name"], job_type=v["job_type"],
                                    params=params, schedules=schedules, **fields)
        else:
            ok, res = self.app.call(orch.create_job, self.app.con, v["name"], v["job_type"], params, lines, **fields)
        if ok:
            self.result = res
            self.win.destroy()
            self.app.refresh()
        return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Orch GUI")
    ap.add_argument("--db", help="path to orch.db (default: $ORCH_DB or orch.db next to orch.py)")
    args = ap.parse_args(argv)
    if args.db:
        os.environ["ORCH_DB"] = os.path.abspath(args.db)
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
