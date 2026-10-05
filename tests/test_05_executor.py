import os
import sys
import threading
import time
import unittest

from helpers import DBTestCase, orch, py_cmd, wait_until

PY = sys.executable


class ExecBase(DBTestCase):
    def start_run(self, job_type, params, timeout=0):
        orch.create_job(self.con, "j", job_type, params, timeout_sec=timeout)
        rid, _ = orch.enqueue(self.con, "j")
        orch.claim_next(self.con, "default")
        return rid

    def execute(self, job_type, params, timeout=0, stop=None):
        rid = self.start_run(job_type, params, timeout)
        spec = orch.build_command(job_type, params)
        res = orch.execute_process(self.con, rid, spec, timeout, stop or threading.Event())
        return rid, res

    def out_lines(self, rid):
        return [l["message"] for l in orch.get_logs(self.con, rid) if l["level"] == "OUT"]


class TestBuildCommand(unittest.TestCase):
    def test_python_and_exec_types(self):
        s = orch.build_command("python", {"file": "a.py", "args": ["x", "y z"]})
        self.assertEqual(s["cmd"], [PY, "-u", "a.py", "x", "y z"])
        self.assertEqual(s["env"]["PYTHONIOENCODING"], "utf-8")
        s = orch.build_command("sql", {"conn": "env:SECRET", "query": "select 1"})
        self.assertEqual(s["cmd"][-2:], ["_exec", "sql"])
        self.assertNotIn("env:SECRET", " ".join(s["cmd"]))  # params travel via env, not argv
        self.assertIn("env:SECRET", s["env"]["ORCH_PARAMS"])

    def test_powershell_flags(self):
        s = orch.build_command("powershell", {"command": "Write-Output 1"})
        self.assertIn("-NoProfile", s["cmd"])
        self.assertIn("-NonInteractive", s["cmd"])
        self.assertEqual(s["cmd"][s["cmd"].index("-ExecutionPolicy") + 1], "Bypass")

    def test_command_shell_and_encoding(self):
        s = orch.build_command("command", {"command": "echo 1", "env": {"A": 1}, "encoding": "cp874"})
        self.assertTrue(s["shell"])
        self.assertEqual(s["encoding"], "cp874")
        self.assertEqual(s["env"]["A"], "1")
        self.assertTrue(orch.build_command("command", {"command": "x"})["encoding"].startswith(
            "cp" if os.name == "nt" else "utf"))


class TestExecutor(ExecBase):
    def test_command_success_captures_output(self):
        rid, res = self.execute("command", {"command": py_cmd("print('a'); print('b')")})
        self.assertEqual((res["outcome"], res["exit_code"]), ("exited", 0))
        self.assertEqual(self.out_lines(rid), ["a", "b"])
        self.assertIsNotNone(self.run_row(rid)["pid"])

    def test_nonzero_exit(self):
        rid, res = self.execute("command", {"command": py_cmd("import sys; print('x'); sys.exit(3)")})
        self.assertEqual((res["exit_code"], res["error"]), (3, "exit code 3"))

    def test_python_thai_utf8_and_cwd_env(self):
        script = os.path.join(self.tmp, "s.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import os, sys\nprint('สวัสดี', os.environ['FOO'], os.getcwd() == sys.argv[1])\n")
        rid, res = self.execute("python", {"file": script, "args": [self.tmp], "env": {"FOO": "bar"},
                                           "cwd": self.tmp})
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(self.out_lines(rid), ["สวัสดี bar True"])

    @unittest.skipUnless(os.name == "nt", "powershell on Windows")
    def test_powershell(self):
        rid, res = self.execute("powershell", {"command": "Write-Output 'ps-ok'; exit 4"})
        self.assertEqual(res["exit_code"], 4)
        self.assertIn("ps-ok", self.out_lines(rid))

    def test_start_failure(self):
        _, res = self.execute("python", {"file": "x.py", "cwd": os.path.join(self.tmp, "missing-dir")})
        self.assertEqual(res["outcome"], "start_failed")
        self.assertIn("cannot start", res["error"])

    def test_output_caps(self):
        code = "print('x' * 3000); [print(i) for i in range(%d)]" % (orch.LOG_MAX_LINES + 50)
        rid, res = self.execute("command", {"command": py_cmd(code)})
        self.assertEqual(res["exit_code"], 0)
        logs = orch.get_logs(self.con, rid)
        outs = [l for l in logs if l["level"] == "OUT"]
        self.assertEqual(len(outs), orch.LOG_MAX_LINES)
        self.assertEqual(len(outs[0]["message"]), orch.LOG_MAX_CHARS)
        warns = [l["message"] for l in logs if l["level"] == "WARN"]
        self.assertEqual(sum("truncated" in w for w in warns), 1)
        self.assertEqual(sum("exceeded" in w for w in warns), 1)

    # --- Test Plan #3 (executor part): timeout kills the whole tree ---
    def test_timeout_kills_process_tree(self):
        pidfile = os.path.join(self.tmp, "child.pid")
        code = ("import subprocess, sys, time; "
                "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                f"open(r'{pidfile}', 'w').write(str(c.pid)); time.sleep(30)")
        t0 = time.time()
        rid, res = self.execute("python", {"file": "-c", "args": [code]}, timeout=4)
        self.assertLess(time.time() - t0, 15)
        self.assertEqual(res["outcome"], "timeout")
        self.assertIn("timeout", res["error"])
        with open(pidfile) as f:
            child = int(f.read())
        wait_until(lambda: not orch.pid_alive(child), 5)
        self.assertFalse(orch.pid_alive(self.run_row(rid)["pid"]))

    def test_timeout_kills_orphaned_grandchild(self):
        """SPEC §11 risk: A starts B, B starts C and exits; C must still die when A is killed.

        taskkill /T alone misses C on Windows (its parent is gone); the WinJob covers it.
        """
        pidfile = os.path.join(self.tmp, "grandchild.pid")
        b_code = ("import subprocess, sys; "
                  "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                  f"open(r'{pidfile}', 'w').write(str(c.pid))")
        a_code = ("import subprocess, sys, time; "
                  f"subprocess.run([sys.executable, '-c', {b_code!r}]); time.sleep(30)")
        t0 = time.time()
        rid, res = self.execute("python", {"file": "-c", "args": [a_code]}, timeout=6)
        self.assertEqual(res["outcome"], "timeout")
        with open(pidfile) as f:
            grandchild = int(f.read())
        self.assertLess(time.time() - t0, 10.5)  # no 5s wait on a pipe held open by the survivor
        self.assertNotIn("still held open", " ".join(l["message"] for l in orch.get_logs(self.con, rid)))
        try:
            wait_until(lambda: not orch.pid_alive(grandchild), 5)
        finally:
            if orch.pid_alive(grandchild):
                orch.kill_tree(grandchild)

    # --- Test Plan #6 (executor part): cancel a running job ---
    def test_cancel_running(self):
        rid = self.start_run("python", {"file": "-c", "args": ["import time; time.sleep(30)"]})
        box = {}
        spec = orch.build_command("python", {"file": "-c", "args": ["import time; time.sleep(30)"]})

        def go():
            c = orch.connect()
            box["res"] = orch.execute_process(c, rid, spec, 0, threading.Event())
            c.close()
        th = threading.Thread(target=go)
        th.start()
        wait_until(lambda: self.run_row(rid)["pid"], 5)
        t0 = time.time()
        orch.cancel_run(self.con, rid)
        th.join(10)
        self.assertLess(time.time() - t0, 3.5)
        self.assertEqual(box["res"]["outcome"], "cancelled")
        self.assertFalse(orch.pid_alive(self.run_row(rid)["pid"]))

    def test_stop_event_shutdown(self):
        stop = threading.Event()
        threading.Timer(0.8, stop.set).start()
        _, res = self.execute("python", {"file": "-c", "args": ["import time; time.sleep(30)"]}, stop=stop)
        self.assertEqual(res["outcome"], "shutdown")

    def test_heartbeat_updates(self):
        old = orch.HEARTBEAT_EVERY
        orch.HEARTBEAT_EVERY = 0.5
        try:
            rid = self.start_run("python", {"file": "-c", "args": ["x"]})
            self.con.execute("UPDATE job_run SET heartbeat='2000-01-01 00:00:00' WHERE id=?", (rid,))
            spec = orch.build_command("python", {"file": "-c", "args": ["import time; time.sleep(1.5)"]})
            orch.execute_process(self.con, rid, spec, 0, threading.Event())
            self.assertGreater(self.run_row(rid)["heartbeat"], "2000-01-01 00:00:00")
        finally:
            orch.HEARTBEAT_EVERY = old

    def test_pid_alive(self):
        self.assertTrue(orch.pid_alive(os.getpid()))
        self.assertFalse(orch.pid_alive(None))


if __name__ == "__main__":
    unittest.main()
