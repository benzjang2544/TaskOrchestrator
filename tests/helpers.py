"""Shared test helpers: temp DB via ORCH_DB, wait_until, engine subprocess."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import orch  # noqa: E402

ORCH_PY = os.path.join(ROOT, "orch.py")
PY = sys.executable


def wait_until(cond, timeout=10.0, interval=0.1):
    """Poll cond() until truthy; return its value or raise AssertionError."""
    deadline = time.time() + timeout
    while True:
        v = cond()
        if v:
            return v
        if time.time() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        time.sleep(interval)


def py_cmd(code: str) -> str:
    """Shell command string running `python -c code` (works on cmd and sh)."""
    return subprocess.list2cmdline([PY, "-c", code]) if os.name == "nt" else \
        " ".join(__import__("shlex").quote(a) for a in [PY, "-c", code])


class DBTestCase(unittest.TestCase):
    """Each test gets its own temp dir + ORCH_DB and a connection `self.con`."""

    def setUp(self):
        # ORCH_TEST_TMP overrides the temp root (e.g. sandboxes that hide %TEMP%). Plain makedirs
        # instead of mkdtemp: on Windows 3.12 mkdtemp's owner-only ACL can hide the dir from children.
        root = os.environ.get("ORCH_TEST_TMP") or tempfile.gettempdir()
        self.tmp = os.path.join(root, f"orch-test-{uuid.uuid4().hex[:12]}")
        os.makedirs(self.tmp)
        self.db = os.path.join(self.tmp, "orch.db")
        self._old_env = os.environ.get("ORCH_DB")
        os.environ["ORCH_DB"] = self.db
        self.con = orch.connect()
        self._engines = []

    def tearDown(self):
        for p in self._engines:
            if p.poll() is None:
                p.kill()
                p.wait(10)
        self.con.close()
        if self._old_env is None:
            os.environ.pop("ORCH_DB", None)
        else:
            os.environ["ORCH_DB"] = self._old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args, check=True):
        """Run `python orch.py args...` against the temp DB; return CompletedProcess."""
        env = dict(os.environ, ORCH_DB=self.db, PYTHONIOENCODING="utf-8")
        r = subprocess.run([PY, ORCH_PY, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=60)
        if check and r.returncode != 0:
            raise AssertionError(f"orch {args} failed rc={r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    def start_engine(self, queue="default", *extra):
        env = dict(os.environ, ORCH_DB=self.db, PYTHONIOENCODING="utf-8")
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        out = open(os.path.join(self.tmp, f"engine-out-{len(self._engines)}.txt"), "wb")
        p = subprocess.Popen([PY, ORCH_PY, "engine", "--queue", queue, "--poll", "0.2", *extra],
                             stdout=out, stderr=subprocess.STDOUT, env=env, **kw)
        out.close()
        p.out_path = out.name
        self._engines.append(p)
        return p

    @staticmethod
    def engine_output(p) -> str:
        with open(p.out_path, encoding="utf-8", errors="replace") as f:
            return f.read()

    @staticmethod
    def stop_engine(p, timeout=20):
        """Graceful stop: CTRL_BREAK on Windows (SIGBREAK), SIGTERM elsewhere."""
        import signal
        p.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
        return p.wait(timeout)

    def status(self, run_id):
        return self.run_row(run_id)["status"]

    def run_row(self, run_id):
        return self.con.execute("SELECT * FROM job_run WHERE id=?", (run_id,)).fetchone()
