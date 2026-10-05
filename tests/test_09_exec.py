"""Step 9: `sql` and `http` job types via `orch.py _exec` (Test Plan #11)."""
import json
import os
import sqlite3
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import DBTestCase, orch

NO_PROXY = {"no_proxy": "*"}  # never route the local test server through a system proxy


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, text, ctype="text/plain; charset=utf-8"):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] == "/ok":
            self._send(200, "สวัสดี ok")
        elif self.path == "/created":
            self._send(201, "made")
        elif self.path == "/headers":
            self._send(200, "token=" + (self.headers.get("X-Token") or ""))
        elif self.path == "/auth":
            self._send(200, "auth=" + (self.headers.get("Authorization") or ""))
        else:
            self._send(500, "boom")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
        self._send(200, f"{self.headers.get('Content-Type')}|{body}")


class ExecCase(DBTestCase):
    def run_job(self, name, job_type, params, **kw):
        orch.create_job(self.con, name, job_type, params, **kw)
        rid, _ = orch.enqueue(self.con, name)
        orch.process_one(self.con, "default", threading.Event())
        return self.run_row(rid)

    def out(self, rid):
        return "\n".join(l["message"] for l in orch.get_logs(self.con, rid) if l["level"] == "OUT")


class TestSql(ExecCase):
    def setUp(self):
        super().setUp()
        self.target = os.path.join(self.tmp, "target.db")
        c = sqlite3.connect(self.target)
        c.execute("CREATE TABLE t(id INTEGER, name TEXT)")
        c.execute("INSERT INTO t VALUES(1, 'หนึ่ง')")
        c.commit()
        c.close()

    def count(self):
        c = sqlite3.connect(self.target)
        try:
            return c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        finally:
            c.close()

    def test_query_success_commits_and_logs_rows(self):
        r = self.run_job("s", "sql", {"conn": f"sqlite:{self.target}",
                                      "query": "INSERT INTO t VALUES(2, 'x;y'); SELECT id, name FROM t ORDER BY id;"})
        self.assertEqual(r["status"], "Succeeded", self.out(r["id"]))
        out = self.out(r["id"])
        self.assertIn("1 row(s) affected", out)
        self.assertIn("id\tname", out)
        self.assertIn("1\tหนึ่ง", out)
        self.assertIn("2\tx;y", out)  # ';' inside a string literal is not a separator
        self.assertIn("(2 rows)", out)
        self.assertEqual(self.count(), 2)

    def test_error_rolls_back(self):
        r = self.run_job("s", "sql", {"conn": f"sqlite:{self.target}",
                                      "query": "INSERT INTO t VALUES(2, 'a'); SELECT * FROM missing_table"})
        self.assertEqual(r["status"], "Dead")
        self.assertEqual(r["exit_code"], 1)
        self.assertIn("SQL error (rolled back)", self.out(r["id"]))
        self.assertEqual(self.count(), 1)

    def test_env_conn_and_query_file_secret_not_logged(self):
        qf = os.path.join(self.tmp, "q.sql")
        with open(qf, "w", encoding="utf-8") as f:
            f.write("SELECT COUNT(*) AS n FROM t;\n")
        conn = f"sqlite:{self.target}"
        r = self.run_job("s", "sql", {"conn": "env:ORCH_TEST_CONN", "query_file": qf,
                                      "env": {"ORCH_TEST_CONN": conn}})
        self.assertEqual(r["status"], "Succeeded", self.out(r["id"]))
        self.assertIn("n\n1", self.out(r["id"]))
        all_logs = " ".join(l["message"] for l in orch.get_logs(self.con, r["id"]))
        self.assertNotIn(self.target, all_logs)

    def test_missing_env_var(self):
        r = self.run_job("s", "sql", {"conn": "env:ORCH_NOT_SET_XYZ", "query": "SELECT 1"})
        self.assertEqual(r["status"], "Dead")
        self.assertIn("ORCH_NOT_SET_XYZ", self.out(r["id"]))

    def test_go_rejected(self):
        r = self.run_job("s", "sql", {"conn": f"sqlite:{self.target}", "query": "SELECT 1\nGO\nSELECT 2"})
        self.assertEqual(r["status"], "Dead")
        self.assertIn("GO", self.out(r["id"]))

    def test_split_statements(self):
        self.assertEqual(orch.split_sqlite_statements("select 1; select ';';;\n select 3"),
                         ["select 1;", "select ';';", "select 3;"])
        self.assertEqual(orch.split_sqlite_statements("select 'unterminated"), ["select 'unterminated"])
        self.assertEqual(orch.split_sqlite_statements(" ; "), [])


class TestHttp(ExecCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def http(self, path, **params):
        return self.run_job("h", "http", dict({"url": self.base + path, "env": NO_PROXY}, **params))

    def test_get_ok(self):
        r = self.http("/ok")
        self.assertEqual(r["status"], "Succeeded", self.out(r["id"]))
        self.assertIn("HTTP 200", self.out(r["id"]))
        self.assertIn("สวัสดี ok", self.out(r["id"]))

    def test_url_secrets_not_logged(self):
        url = self.base.replace("http://", "http://user:pw123@") + "/ok?token=SECRET123"
        r = self.run_job("h", "http", {"url": url, "env": NO_PROXY})
        self.assertEqual(r["status"], "Succeeded", self.out(r["id"]))
        logs = " ".join(l["message"] for l in orch.get_logs(self.con, r["id"]))
        self.assertNotIn("SECRET123", logs)
        self.assertNotIn("pw123", logs)
        self.assertIn("/ok?***", logs)
        r = self.run_job("h2", "http", {"url": self.base.replace("http://", "http://user:pw123@") + "/auth",
                                        "env": NO_PROXY})
        self.assertIn("auth=Basic dXNlcjpwdzEyMw==", self.out(r["id"]))  # base64("user:pw123")

    def test_server_error_fails(self):
        r = self.http("/fail")
        self.assertEqual(r["status"], "Dead")
        self.assertIn("HTTP 500", self.out(r["id"]))
        self.assertIn("not in expected", self.out(r["id"]))

    def test_expect(self):
        self.assertEqual(self.http("/fail", expect="500")["status"], "Succeeded")

    def test_expect_list_excludes_201(self):
        r = self.http("/created", expect="200")
        self.assertEqual(r["status"], "Dead")
        self.assertIn("HTTP 201", self.out(r["id"]))

    def test_post_json_body_and_headers(self):
        r = self.http("/echo", body={"a": "ข"})
        self.assertEqual(r["status"], "Succeeded")
        ctype, _, body = self.out(r["id"]).splitlines()[-1].partition("|")
        self.assertIn("application/json", ctype)
        self.assertEqual(json.loads(body), {"a": "ข"})
        r2_params = {"url": self.base + "/headers", "headers": {"X-Token": "abc"}, "env": NO_PROXY}
        orch.create_job(self.con, "h2", "http", r2_params)
        rid, _ = orch.enqueue(self.con, "h2")
        orch.process_one(self.con, "default", threading.Event())
        self.assertIn("token=abc", self.out(rid))

    def test_connection_refused(self):
        s = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = s.server_address[1]
        s.server_close()  # nothing listens there now
        r = self.run_job("h", "http", {"url": f"http://127.0.0.1:{port}/", "timeout": 5, "env": NO_PROXY})
        self.assertEqual(r["status"], "Dead")
        self.assertIn("request failed", self.out(r["id"]))

    def test_exec_hidden_from_help(self):
        self.assertNotIn("_exec", self.cli("--help").stdout)


if __name__ == "__main__":
    unittest.main()
