"""tools/backup_db.py: online copy of orch.db + retention."""
import datetime
import os
import sqlite3
import subprocess

from helpers import DBTestCase, PY, ROOT, orch

BACKUP_PY = os.path.join(ROOT, "tools", "backup_db.py")


def _load_tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location("backup_db", BACKUP_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestBackup(DBTestCase):
    def test_cli_uses_orch_db_and_copy_is_complete(self):
        orch.create_job(self.con, name="j1", job_type="command", params={"command": "echo hi"})
        env = dict(os.environ, ORCH_DB=self.db, PYTHONIOENCODING="utf-8")
        r = subprocess.run([PY, BACKUP_PY], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        files = os.listdir(os.path.join(self.tmp, "backup"))
        self.assertEqual(files, [f"orch-{datetime.date.today():%Y%m%d}.db"])
        copy = sqlite3.connect(os.path.join(self.tmp, "backup", files[0]))
        try:
            tables = {r[0] for r in copy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"job_def", "job_run", "run_log", "engine_lock"} <= tables)
            self.assertEqual(copy.execute("SELECT name FROM job_def").fetchall(), [("j1",)])
        finally:
            copy.close()

    def test_keeps_newest_n(self):
        tool = _load_tool()
        dest = os.path.join(self.tmp, "bk")
        for d in range(1, 6):
            tool.backup(self.db, dest, keep=3, today=datetime.date(2026, 10, d))
        self.assertEqual(sorted(os.listdir(dest)),
                         ["orch-20261003.db", "orch-20261004.db", "orch-20261005.db"])

    def test_missing_db_fails(self):
        tool = _load_tool()
        with self.assertRaises(FileNotFoundError):
            tool.backup(os.path.join(self.tmp, "nope.db"), os.path.join(self.tmp, "bk"), keep=3)
