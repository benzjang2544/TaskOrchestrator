"""Step 11: install-startup script generation, --db, launchers, Thai output encoding, pythonw engine."""
import contextlib
import io
import os
import subprocess
import threading
import unittest
from unittest import mock

from helpers import DBTestCase, ORCH_PY, ROOT, orch, wait_until

THAI = "สวัสดี"


class TestInstallStartup(DBTestCase):
    def test_boot_system_script(self):
        s = orch.build_startup_script("etl", "boot")
        for must in ("-ExecutionTimeLimit ([TimeSpan]::Zero)", "-RestartCount 999",
                     "-RestartInterval (New-TimeSpan -Minutes 1)", "-MultipleInstances IgnoreNew",
                     "-StartWhenAvailable", "New-ScheduledTaskTrigger -AtStartup", "-UserId 'SYSTEM'",
                     "Register-ScheduledTask", "'Orch Engine (etl)'", "--queue etl"):
            self.assertIn(must, s)
        self.assertIn(f'--db "{self.db}"' if " " in self.db else f"--db {self.db}", s)
        self.assertNotIn("schtasks", s.lower())

    def test_boot_with_user_asks_password_not_embedded(self):
        s = orch.build_startup_script("q", "boot", "DOMAIN\\svc")
        self.assertIn("Get-Credential -UserName 'DOMAIN\\svc'", s)
        self.assertIn("-Password $cred.GetNetworkCredential().Password", s)
        self.assertNotIn("SYSTEM", s)

    def test_logon_script(self):
        s = orch.build_startup_script("ui", "logon")
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn -User $user", s)
        self.assertIn("-LogonType Interactive", s)
        self.assertIn("$env:USERNAME", s)
        self.assertIn("$user = 'bob'", orch.build_startup_script("ui", "logon", "bob"))

    def test_every_variant_relaunches_a_dead_engine(self):
        for s in (orch.build_startup_script("q", "boot"), orch.build_startup_script("q", "boot", "D\\svc"),
                  orch.build_startup_script("q", "logon")):
            self.assertIn("-RepetitionInterval (New-TimeSpan -Minutes 5)", s)
            self.assertIn("-Trigger @($trigger, $repeat)", s)
            self.assertIn("-MultipleInstances IgnoreNew", s)  # what makes repeated launches harmless

    def test_quotes_escaped(self):
        self.assertIn("'Orch Engine (it''s)'", orch.build_startup_script("it's", "boot"))

    def test_cli_prints_without_apply(self):
        out = self.cli("install-startup", "--queue", "q1", "--trigger", "logon").stdout  # never registers
        self.assertIn("Register-ScheduledTask", out)
        self.assertIn("Unregister-ScheduledTask", out)
        self.assertEqual(self.cli("install-startup", "--trigger", "never", check=False).returncode, 2)

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_apply_runs_powershell_file(self):
        seen = {}

        def fake_run(cmd, *a, **kw):
            seen["cmd"] = cmd
            with open(cmd[-1], encoding="utf-8-sig") as f:
                seen["script"] = f.read()
            return subprocess.CompletedProcess(cmd, 0)
        with mock.patch.object(orch.subprocess, "run", side_effect=fake_run),                 contextlib.redirect_stdout(io.StringIO()) as out:  # mocked: nothing is really registered
            self.assertEqual(orch.main(["install-startup", "--trigger", "boot", "--apply"]), 0)
        self.assertIn("registered task", out.getvalue())
        self.assertEqual(seen["cmd"][:2], ["powershell", "-NoProfile"])
        self.assertIn("Register-ScheduledTask", seen["script"])
        self.assertFalse(os.path.exists(seen["cmd"][-1]))  # temp script removed

    def test_global_db_option(self):
        other = os.path.join(self.tmp, "other.db")
        env = dict(os.environ)
        env.pop("ORCH_DB", None)
        r = subprocess.run([orch.sys.executable, ORCH_PY, "--db", other, "init"], capture_output=True, text=True,
                           env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(other, r.stdout)
        self.assertTrue(os.path.exists(other))

    def test_bat_launchers_exist_ascii_crlf(self):
        for name in ("orch.bat", "orch_gui.bat", "orch_engine.bat", "start_orch.bat", "setup.bat",
                     "uninstall.bat", "package.bat"):
            with open(os.path.join(ROOT, name), "rb") as f:
                data = f.read()
            data.decode("ascii")
            self.assertIn(b"\r\n", data)
            self.assertNotIn(b"\n", data.replace(b"\r\n", b""))


# orch.py verbs each installer script must keep calling (catches a rename in orch.py)
SETUP_SCRIPTS = {"setup.ps1": ("'init'", "'install-startup'", "'stop-engine'", "'--apply'"),
                 "uninstall.ps1": ("stop-engine", "Unregister-ScheduledTask", "orch_gui", "Start-Transcript")}


def _is_admin() -> bool:
    import ctypes
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


@unittest.skipUnless(os.name == "nt", "PowerShell installer scripts are Windows only")
class TestSetupScripts(DBTestCase):
    def test_utf8_bom_and_verbs(self):
        # PowerShell 5.1 reads a BOM-less script as ANSI, which would garble the Thai messages
        for name, verbs in SETUP_SCRIPTS.items():
            with open(os.path.join(ROOT, name), "rb") as f:
                data = f.read()
            self.assertTrue(data.startswith(b"\xef\xbb\xbf"), name)
            text = data.decode("utf-8-sig")
            for verb in verbs:
                self.assertIn(verb, text, name)

    def test_powershell_parses(self):
        for name in SETUP_SCRIPTS:
            path = os.path.join(ROOT, name)
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                                f"'{path}', [ref]$null, [ref]$e); $e.Count"],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.stdout.strip(), "0", f"{name}: {r.stdout}{r.stderr}")

    def test_install_acl(self):
        # Run setup.ps1's own Set-InstallAcl (taken from the script's AST) on a temp folder: Authenticated Users
        # must lose write access (the engine runs this folder's code as SYSTEM), the setup account keeps it.
        d = os.path.join(self.tmp, "inst")
        os.makedirs(os.path.join(d, "logs"))
        with open(os.path.join(d, "orch.db"), "w") as f:
            f.write("x")
        script = os.path.join(ROOT, "setup.ps1")
        ps = ("$ErrorActionPreference = 'Stop'; $e = $null; "
              f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$null, [ref]$e); "
              "foreach ($f in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) "
              "{ if ('Invoke-Exe', 'Set-InstallAcl' -contains $f.Name) { . ([scriptblock]::Create($f.Extent.Text)) } }; "
              "$me = [Security.Principal.WindowsIdentity]::GetCurrent().Name; "
              # what C:\ gives a new folder, plus an explicit entry on the file: both must go
              f"icacls '{d}' /grant '*S-1-5-11:(OI)(CI)M' /Q | Out-Null; "
              f"icacls '{os.path.join(d, 'orch.db')}' /grant '*S-1-5-11:M' /Q | Out-Null; "
              f"Set-InstallAcl '{d}' @($me); "
              "foreach ($p in @('" + d + "', '" + os.path.join(d, "orch.db") + "')) { "
              "(Get-Acl $p).Access | ForEach-Object { '{0}|{1}|{2}' -f $p, $_.IdentityReference.Translate("
              "[Security.Principal.SecurityIdentifier]).Value, $_.FileSystemRights } }; "
              "'ME|' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = [l.split("|") for l in r.stdout.splitlines() if "|" in l]
        me = next(l[1] for l in lines if l[0] == "ME")
        rights = [(sid, right) for p, sid, right in (l for l in lines if l[0] != "ME")]
        sids = {sid for sid, _ in rights}
        self.assertNotIn("S-1-5-11", sids, r.stdout)  # Authenticated Users
        self.assertIn("S-1-5-18", sids)
        self.assertIn("S-1-5-32-544", sids)
        for sid, right in rights:
            if sid == "S-1-5-32-545":  # Users: read only
                self.assertNotIn("Modify", right)
                self.assertNotIn("Write", right)
        self.assertTrue(any(sid == me and "Modify" in right for sid, right in rights), r.stdout)
        with open(os.path.join(d, "orch.db"), "a") as f:  # the setup account can still write
            f.write("y")

    def run_setup_functions(self, names, body):
        """Load setup.ps1's own functions `names` (from the script's AST) and run `body`; returns stdout lines."""
        script = os.path.join(ROOT, "setup.ps1")
        quoted = ", ".join(f"'{n}'" for n in names)
        ps = ("$ErrorActionPreference = 'Stop'; $e = $null; "
              f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$null, [ref]$e); "
              "foreach ($f in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) "
              f"{{ if (@({quoted}) -contains $f.Name) {{ . ([scriptblock]::Create($f.Extent.Text)) }} }}; " + body)
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout.splitlines()

    def test_session_user_is_the_signed_in_user(self):
        # a logon task must belong to the user at the screen, not to the admin typed at the UAC prompt;
        # unelevated, the owner of this session's explorer.exe is simply this user
        if not subprocess.run(["tasklist", "/FI", "IMAGENAME eq explorer.exe"], capture_output=True,
                              text=True).stdout.lower().count("explorer.exe"):
            self.skipTest("no explorer.exe (no interactive session)")
        out = self.run_setup_functions(["Get-SessionUser"],
                                       "'USER|' + (Get-SessionUser); 'ME|' + [Security.Principal.WindowsIdentity]::GetCurrent().Name")
        got = dict(l.split("|", 1) for l in out if "|" in l)
        self.assertEqual(got["USER"].lower(), got["ME"].lower())

    def test_logon_task_users_runs(self):
        out = self.run_setup_functions(["Get-LogonTaskUsers"], "'N|' + @(Get-LogonTaskUsers 'Orch Engine (x)').Count")
        self.assertTrue(any(l.startswith("N|") for l in out), out)

    def test_setup_logon_user_wiring(self):
        with open(os.path.join(ROOT, "setup.ps1"), encoding="utf-8-sig") as f:
            text = f.read()
        self.assertIn("[string]$User", text)
        self.assertIn("$startup += @('--user', $EngineUser)", text)
        self.assertIn("@($EngineUser) + @(Get-LogonTaskUsers $TaskName)", text)  # its user can write orch.db

    @unittest.skipIf(os.name == "nt" and _is_admin(), "elevated: the script would really install")
    def test_scripts_refuse_without_admin(self):
        target = os.path.join(self.tmp, "install")
        for name in SETUP_SCRIPTS:
            r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                os.path.join(ROOT, name), "-InstallDir", target],
                               capture_output=True, timeout=60)
            self.assertEqual(r.returncode, 1, name)
            self.assertFalse(os.path.exists(target), name)  # nothing touched before the admin check


@unittest.skipUnless(os.name == "nt", "Windows console encodings")
class TestThaiOutput(DBTestCase):
    """SPEC §11 risk: Thai text in logs via cmd, PowerShell and Python."""

    def run_capture(self, job_type, params):
        orch.create_job(self.con, "t", job_type, params)
        rid, _ = orch.enqueue(self.con, "t")
        orch.process_one(self.con, "default", threading.Event())
        return [l["message"] for l in orch.get_logs(self.con, rid) if l["level"] == "OUT"]

    def oem_has_thai(self):
        try:
            THAI.encode(orch.default_encoding())
            return True
        except UnicodeEncodeError:
            return False

    def test_python_thai(self):
        self.assertEqual(self.run_capture("python", {"file": "-c", "args": [f"print('{THAI}')"]}), [THAI])

    def test_cmd_thai(self):
        if not self.oem_has_thai():
            self.skipTest(f"OEM code page {orch.default_encoding()} cannot represent Thai")
        self.assertEqual(self.run_capture("command", {"command": f"echo {THAI}"}), [THAI])

    def test_powershell_thai(self):
        if not self.oem_has_thai():
            self.skipTest(f"OEM code page {orch.default_encoding()} cannot represent Thai")
        self.assertIn(THAI, self.run_capture("powershell", {"command": f"Write-Output '{THAI}'"}))

    def test_utf8_encoding_override(self):
        # a program that writes UTF-8 bytes regardless of the console code page
        code = f"import sys; sys.stdout.buffer.write('{THAI}\\n'.encode('utf-8'))"
        cmd = subprocess.list2cmdline([orch.sys.executable, "-c", code])
        self.assertEqual(self.run_capture("command", {"command": cmd, "encoding": "utf-8"}), [THAI])


@unittest.skipUnless(os.name == "nt", "pythonw is Windows only")
class TestPythonwEngine(DBTestCase):
    def test_engine_under_pythonw(self):
        exe = orch.engine_executable()
        self.assertTrue(exe.lower().endswith("pythonw.exe"))
        orch.create_job(self.con, "a", "python", {"file": "-c", "args": ["print('hi')"]})
        rid, _ = orch.enqueue(self.con, "a")
        env = dict(os.environ)
        env.pop("ORCH_DB", None)  # must come from --db, like the scheduled task
        p = subprocess.Popen([exe, ORCH_PY, "--db", self.db, "engine", "--queue", "default"], env=env,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        self._engines.append(p)
        wait_until(lambda: self.status(rid) == "Succeeded", 20)
        with open(os.path.join(self.tmp, "logs", "engine-default.log"), encoding="utf-8") as f:
            self.assertIn("Succeeded", f.read())


if __name__ == "__main__":
    unittest.main()
