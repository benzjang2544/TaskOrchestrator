<#
  Orch setup: install or update Orch on this machine (run via setup.bat, which asks for Administrator).

  1. find an all-users Python >= 3.10 (HKLM), installing Python 3.12 if missing
     (python-3.*-amd64.exe next to this script = offline, otherwise winget)
  2. pyodbc for that Python (machine-wide, so a SYSTEM engine sees it; *.whl next to this script = offline)
  3. update: disable the engine task and stop the engine gracefully (stop-engine)
  4. copy the files to -InstallDir (never deletes; orch.db and logs are kept)
  5. orch.py init (creates the DB or migrates an existing one)
  6. orch.py install-startup --apply, start the task, wait for the engine
  7. GUI shortcuts (all-users Desktop + Start menu) and -InstallDir on the machine PATH

  Step 4 also locks down -InstallDir: the engine runs orch.py and the jobs in orch.db as SYSTEM, so anyone who can
  write there can run code as SYSTEM. Only SYSTEM, Administrators, the account running setup and -AllowUser may write;
  other users can only read.

  Example: setup.bat -Queue ui -Trigger logon
           setup.bat -AllowUser DOMAIN\opsuser   (that account effectively gets admin rights on this machine)
#>
param(
    [string]$InstallDir = 'C:\Orch',
    [string]$Queue = 'default',
    [ValidateSet('boot', 'logon')][string]$Trigger = 'boot',
    [string]$ShortcutName = 'Orch',
    [string[]]$AllowUser = @()
)

$ErrorActionPreference = 'Stop'
$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$Src = $PSScriptRoot.TrimEnd('\')
$OrchPy = Join-Path $InstallDir 'orch.py'
$Db = Join-Path $InstallDir 'orch.db'
$TaskName = "Orch Engine ($Queue)"
$EnvKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
$script:Step = 'เริ่มต้น'
$script:DisabledTask = $false

function Step([string]$Text) {
    $script:Step = $Text
    Write-Host ''
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Ok([string]$Text) { Write-Host "    OK  $Text" -ForegroundColor Green }
function Warn([string]$Text) { Write-Host "    !!  $Text" -ForegroundColor Yellow }

function Invoke-Exe([string]$Exe, [string[]]$ArgList, [int[]]$OkCodes = @(0)) {
    # Native output goes through the pipeline so Start-Transcript records it; stderr must not
    # become a terminating error, so relax $ErrorActionPreference just for this call.
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @ArgList 2>&1 | ForEach-Object { "    $_" } | Out-Host
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    if ($OkCodes -notcontains $code) {
        throw "$([IO.Path]::GetFileName($Exe)) จบด้วย exit code $code"
    }
    return $code
}

function Get-PyOutput([string[]]$ArgList) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { return (& $script:Py @ArgList 2>$null | Out-String).Trim() } finally { $ErrorActionPreference = $old }
}

function Find-Python {
    # All-users installs only (HKLM): an engine running as SYSTEM can't rely on a per-user Python.
    $best = $null
    foreach ($k in Get-ChildItem 'HKLM:\SOFTWARE\Python\PythonCore' -ErrorAction SilentlyContinue) {
        if ($k.PSChildName -notmatch '^(\d+)\.(\d+)$') { continue }
        $ver = [version]"$($Matches[1]).$($Matches[2])"
        if ($ver -lt [version]'3.10') { continue }
        $ip = Get-ItemProperty -Path (Join-Path $k.PSPath 'InstallPath') -ErrorAction SilentlyContinue
        if (-not $ip) { continue }
        $exe = $ip.ExecutablePath
        if (-not $exe -and $ip.'(default)') { $exe = Join-Path $ip.'(default)' 'python.exe' }
        if ($exe -and (Test-Path $exe) -and (-not $best -or $ver -gt $best.Version)) {
            $best = [pscustomobject]@{ Version = $ver; Exe = $exe }
        }
    }
    return $best
}

function Set-InstallAcl([string]$Dir, [string[]]$Writers) {
    # Well-known SIDs, so this works whatever the Windows display language is.
    # /reset drops explicit entries someone added to the folder, then /inheritance:r drops what C:\ hands down
    # (Authenticated Users: Modify), so only the grants below are left.
    $grants = @('*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-32-545:(OI)(CI)RX')
    foreach ($w in $Writers) { $grants += "${w}:(OI)(CI)M" }
    Invoke-Exe 'icacls.exe' @($Dir, '/reset', '/Q') | Out-Null
    Invoke-Exe 'icacls.exe' (@($Dir, '/inheritance:r', '/grant:r') + $grants + @('/Q')) | Out-Null
    # Children must not keep explicit entries of their own (e.g. a writable orch.db): make them inherit only.
    Invoke-Exe 'icacls.exe' @((Join-Path $Dir '*'), '/reset', '/T', '/C', '/Q') | Out-Null
}

function Send-EnvChange {
    if (-not ('Win32.OrchEnv' -as [type])) {
        Add-Type -Namespace Win32 -Name OrchEnv -MemberDefinition @'
[DllImport("user32.dll", CharSet = CharSet.Unicode)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam,
    uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@
    }
    $r = [UIntPtr]::Zero
    [void][Win32.OrchEnv]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$r)
}

function Add-MachinePath([string]$Dir) {
    # Read the raw REG_EXPAND_SZ (keeps %SystemRoot% etc.); setx would truncate a PATH over 1024 chars.
    $raw = (Get-Item $EnvKey).GetValue('Path', '', 'DoNotExpandEnvironmentNames')
    $parts = @($raw -split ';' | Where-Object { $_ })
    if ($parts | Where-Object { $_.TrimEnd('\') -ieq $Dir.TrimEnd('\') }) { return $false }
    Set-ItemProperty -Path $EnvKey -Name Path -Value (($parts + $Dir) -join ';') -Type ExpandString
    return $true
}

# ---------------------------------------------------------------------------

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'ต้องรันด้วยสิทธิ์ Administrator: ดับเบิลคลิก setup.bat แล้วกด Yes ที่หน้าต่าง UAC' -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir 'logs') | Out-Null
$LogFile = Join-Path $InstallDir ("logs\setup-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
Start-Transcript -Path $LogFile | Out-Null

Write-Host "Orch setup: ติดตั้งที่ $InstallDir, คิว '$Queue', เปิด engine ตอน $Trigger"

try {
    Step 'ตรวจหา Python 3.10 ขึ้นไปแบบติดตั้งให้ทุกคน (all users)'
    $found = Find-Python
    if ($found) {
        Ok "พบ Python $($found.Version) ที่ $($found.Exe)"
    } else {
        $override = '/quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1'
        $local = Get-ChildItem -Path $Src -Filter 'python-3.*-amd64.exe' -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending | Select-Object -First 1
        if ($local) {
            Write-Host "    ติดตั้ง $($local.Name) จากโฟลเดอร์นี้ (offline)..."
            $p = Start-Process -FilePath $local.FullName -ArgumentList $override -Wait -PassThru
            if ($p.ExitCode -ne 0) { throw "ตัวติดตั้ง Python จบด้วย exit code $($p.ExitCode)" }
        } elseif (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Host '    ติดตั้ง Python 3.12 ด้วย winget (ต้องต่ออินเทอร์เน็ต อาจใช้เวลาหลายนาที)...'
            Invoke-Exe 'winget' @('install', '-e', '--id', 'Python.Python.3.12', '--scope', 'machine',
                '--accept-package-agreements', '--accept-source-agreements', '--override', $override) | Out-Null
        } else {
            throw ('ไม่พบ Python และเครื่องนี้ไม่มี winget: ดาวน์โหลด python-3.12.x-amd64.exe จาก python.org ' +
                'มาวางไว้โฟลเดอร์เดียวกับ setup.bat แล้วรันใหม่')
        }
        $found = Find-Python
        if (-not $found) { throw 'ติดตั้ง Python แล้วแต่ยังหาไม่พบใน registry (HKLM) ให้ติดตั้งเองแบบ "Install for all users"' }
        Ok "ติดตั้ง Python $($found.Version) แล้วที่ $($found.Exe)"
    }
    $script:Py = $found.Exe
    $PyDir = Split-Path $script:Py
    $Pyw = Join-Path $PyDir 'pythonw.exe'

    Step 'ติดตั้ง pyodbc (ใช้กับงาน sql ที่ต่อ SQL Server)'
    # -s ignores this user's own site-packages: otherwise a per-user pyodbc makes pip say "already satisfied"
    # and skip the machine-wide install that the SYSTEM engine needs
    $code = Invoke-Exe $script:Py @('-s', '-m', 'pip', 'install', '--disable-pip-version-check', '--no-warn-script-location',
        '--timeout', '15', '--retries', '1', '--find-links', $Src, 'pyodbc') -OkCodes (0..255)
    $where = Get-PyOutput @('-s', '-c', 'import sys, pyodbc; print(pyodbc.__file__.lower().startswith(sys.prefix.lower()))')
    $pyodbcOk = ($where -eq 'True')
    if ($pyodbcOk) {
        Ok 'pyodbc พร้อมใช้ (ติดตั้งให้ทั้งเครื่อง)'
    } elseif ($where -eq 'False') {
        Warn 'pyodbc ถูกติดตั้งแบบรายผู้ใช้ engine ที่รันในนาม SYSTEM จะมองไม่เห็น (งาน sql กับ SQL Server จะใช้ไม่ได้)'
    } else {
        Warn "ติดตั้ง pyodbc ไม่สำเร็จ (exit code $code) งานอื่นใช้ได้ตามปกติ ยกเว้นงาน sql กับ SQL Server"
        Warn 'ถ้าเครื่องไม่มีอินเทอร์เน็ต ให้วางไฟล์ pyodbc-*.whl ไว้ข้าง setup.bat แล้วรันใหม่'
    }

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Step "หยุด engine เดิมของคิว '$Queue' เพื่ออัปเดต"
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        $script:DisabledTask = $true
        if (Test-Path $OrchPy) {
            # exit 2 = no engine running (or an old orch.py without stop-engine): fine either way
            Invoke-Exe $script:Py @($OrchPy, '--db', $Db, 'stop-engine', '--queue', $Queue, '--wait', '60') -OkCodes @(0, 2) | Out-Null
        }
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Ok 'engine เดิมหยุดแล้ว (งานที่รันอยู่กลับเป็น Pending)'
    }

    Step "คัดลอกไฟล์ไปที่ $InstallDir"
    if ($Src -ieq $InstallDir) {
        Ok 'รันจากโฟลเดอร์ติดตั้งอยู่แล้ว ไม่ต้องคัดลอก'
    } else {
        # /E without /MIR: never deletes anything at the destination (orch.db, logs, user files stay)
        Invoke-Exe 'robocopy.exe' @($Src, $InstallDir, '/E', '/R:2', '/W:2', '/NFL', '/NDL', '/NJH', '/NP',
            '/XD', '.git', '__pycache__', 'logs', 'backup', 'dist',
            '/XF', 'orch.db', 'orch.db-wal', 'orch.db-shm', 'python-*.exe', '*.whl') -OkCodes (0..7) | Out-Null
        Ok 'คัดลอกแล้ว (ฐานข้อมูลและ log เดิมไม่ถูกแตะ)'
    }

    Step "จำกัดสิทธิ์เขียนโฟลเดอร์ $InstallDir"
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    # powershell -File passes "-AllowUser a,b" as one string, not an array
    $writers = @($me) + @($AllowUser | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    Set-InstallAcl $InstallDir $writers
    Ok "เขียนได้เฉพาะ SYSTEM, Administrators, $($writers -join ', ') (ผู้ใช้อื่นอ่านได้อย่างเดียว)"
    if ($AllowUser) { Warn 'account ใน -AllowUser สั่งงานให้รันในนาม SYSTEM ได้ จึงเท่ากับมีสิทธิ์ admin ของเครื่องนี้' }

    Step 'เตรียมฐานข้อมูล'
    Invoke-Exe $script:Py @($OrchPy, '--db', $Db, 'init') | Out-Null
    Ok $Db

    Step "ลงทะเบียน engine กับ Task Scheduler ('$TaskName')"
    Invoke-Exe $script:Py @($OrchPy, '--db', $Db, 'install-startup', '--queue', $Queue, '--trigger', $Trigger, '--apply') | Out-Null
    $script:DisabledTask = $false  # re-registered with -Force: enabled again
    $task = Get-ScheduledTask -TaskName $TaskName
    if ($task.Settings.ExecutionTimeLimit -eq 'PT0S') { Ok 'ไม่มีเวลาหมดอายุ (ExecutionTimeLimit = PT0S)' }
    else { Warn "ExecutionTimeLimit = $($task.Settings.ExecutionTimeLimit) (ควรเป็น PT0S)" }
    if ($task.Triggers.Count -eq 2) { Ok 'มี trigger 2 ตัว (ตอนเริ่มต้น + เปิดซ้ำทุก 5 นาที)' }
    else { Warn "มี trigger $($task.Triggers.Count) ตัว (ควรเป็น 2)" }

    Step 'เริ่ม engine'
    Start-ScheduledTask -TaskName $TaskName
    $check = ('import os, sys; os.environ[''ORCH_DB''] = sys.argv[1]; sys.path.insert(0, sys.argv[2]); import orch; ' +
        's = [x for x in orch.engine_states(orch.connect()) if x[''queue''] == sys.argv[3]]; ' +
        'print(''running'' if s and s[0][''running''] else ''stopped'')')
    $engineOk = $false
    for ($i = 0; $i -lt 30 -and -not $engineOk; $i++) {
        Start-Sleep -Seconds 1
        $engineOk = ((Get-PyOutput @('-c', $check, $Db, $InstallDir, $Queue)) -eq 'running')
    }
    if ($engineOk) { Ok "engine ของคิว '$Queue' ทำงานแล้ว" }
    elseif ($Trigger -eq 'logon') { Warn 'engine ยังไม่เริ่ม (แบบ logon จะเริ่มเมื่อ user ที่ลงทะเบียนไว้ login)' }
    else { Warn "engine ยังไม่เริ่มภายใน 30 วินาที ดู $InstallDir\logs\engine-$Queue.log" }

    Step 'สร้าง shortcut ของ GUI'
    $ws = New-Object -ComObject WScript.Shell
    foreach ($dir in @([Environment]::GetFolderPath('CommonDesktopDirectory'), [Environment]::GetFolderPath('CommonPrograms'))) {
        $path = Join-Path $dir "$ShortcutName.lnk"
        $lnk = $ws.CreateShortcut($path)
        $lnk.TargetPath = $Pyw
        $lnk.Arguments = "`"$InstallDir\orch_gui.py`" --db `"$Db`""
        $lnk.WorkingDirectory = $InstallDir
        $lnk.IconLocation = "$Pyw,0"
        $lnk.Description = 'Orch - job orchestrator'
        $lnk.Save()
        Ok $path
    }

    Step 'เพิ่ม PATH ของเครื่อง'
    $changed = $false
    foreach ($dir in @($InstallDir, $PyDir)) {
        if (Add-MachinePath $dir) { Ok "เพิ่ม $dir"; $changed = $true } else { Ok "มี $dir อยู่แล้ว" }
    }
    if ($changed) { Send-EnvChange }

    Write-Host ''
    Write-Host '==================== ติดตั้งเสร็จแล้ว ====================' -ForegroundColor Green
    Write-Host "  โฟลเดอร์       : $InstallDir"
    Write-Host "  Python         : $($found.Version)  ($script:Py)"
    Write-Host "  pyodbc         : $(if ($pyodbcOk) { 'พร้อมใช้' } else { 'ไม่มี (งาน sql กับ SQL Server ใช้ไม่ได้)' })"
    Write-Host "  engine         : $(if ($engineOk) { 'ทำงานอยู่' } else { 'ยังไม่ทำงาน' })  (task '$TaskName')"
    Write-Host "  GUI            : ไอคอน '$ShortcutName' บน Desktop หรือใน Start menu"
    Write-Host '  CLI            : เปิดหน้าต่าง cmd/PowerShell ใหม่แล้วพิมพ์  orch query'
    Write-Host "  log การติดตั้ง : $LogFile"
    $rc = 0
} catch {
    Write-Host ''
    Write-Host "ติดตั้งไม่สำเร็จที่ขั้น: $script:Step" -ForegroundColor Red
    Write-Host "  $($_.Exception.Message)" -ForegroundColor Red
    if ($script:DisabledTask) {
        # update failed midway: bring the previous engine back rather than leaving the queue without one
        Enable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
        Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Warn 'เปิด engine เดิมกลับแล้ว'
    }
    Write-Host "  ดูรายละเอียดใน $LogFile"
    $rc = 1
} finally {
    Stop-Transcript | Out-Null
}
exit $rc
