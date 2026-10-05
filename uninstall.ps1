<#
  Orch uninstall (run via uninstall.bat, which asks for Administrator).

  Removes what setup.ps1 added for the installation in -InstallDir (default: this script's folder):
  its engine tasks (after a graceful stop-engine), the GUI shortcuts and the PATH entry.
  Keeps the folder, orch.db and logs: running setup.bat again brings back every job and its history.
  Only touches tasks/shortcuts that point at this folder (shortcuts whatever their name, so a
  `setup.bat -ShortcutName X` install needs no option here), so other Orch installations are left alone.
  Logs to <InstallDir>\logs\uninstall-<time>.log.
#>
param(
    [string]$InstallDir = $PSScriptRoot
)

$ErrorActionPreference = 'Stop'
$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$OrchPy = Join-Path $InstallDir 'orch.py'
$Db = Join-Path $InstallDir 'orch.db'
$EnvKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment'

function Ok([string]$Text) { Write-Host "    OK  $Text" -ForegroundColor Green }
function Warn([string]$Text) { Write-Host "    !!  $Text" -ForegroundColor Yellow }

function Refers-ToInstall([string]$Text) {
    return $Text -and $Text.IndexOf("$InstallDir\", [StringComparison]::OrdinalIgnoreCase) -ge 0
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

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'ต้องรันด้วยสิทธิ์ Administrator: ดับเบิลคลิก uninstall.bat แล้วกด Yes ที่หน้าต่าง UAC' -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir 'logs') | Out-Null
$LogFile = Join-Path $InstallDir ("logs\uninstall-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
Start-Transcript -Path $LogFile | Out-Null

Write-Host "Orch uninstall: $InstallDir"
$rc = 0
try {
    Write-Host ''
    Write-Host '==> หยุดและลบ engine ออกจาก Task Scheduler' -ForegroundColor Cyan
    $tasks = @(Get-ScheduledTask -TaskName 'Orch Engine (*)' -ErrorAction SilentlyContinue |
        Where-Object { Refers-ToInstall $_.Actions[0].Arguments })
    if (-not $tasks) { Ok 'ไม่มี task ของโฟลเดอร์นี้' }
    foreach ($t in $tasks) {
        Disable-ScheduledTask -TaskName $t.TaskName | Out-Null  # the 5-minute trigger must not relaunch it
        $queue = if ($t.TaskName -match '^Orch Engine \((.*)\)$') { $Matches[1] } else { 'default' }
        # the task runs pythonw.exe; python.exe next to it shows stop-engine's output here
        $py = Join-Path (Split-Path $t.Actions[0].Execute.Trim('"')) 'python.exe'
        if ((Test-Path $py) -and (Test-Path $OrchPy)) {
            $old = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            try {
                & $py $OrchPy --db $Db stop-engine --queue $queue --wait 60 2>&1 | ForEach-Object { "    $_" } | Out-Host
            } finally { $ErrorActionPreference = $old }
        }
        Stop-ScheduledTask -TaskName $t.TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false
        Ok "ลบ '$($t.TaskName)' แล้ว"
    }

    Write-Host ''
    Write-Host '==> ลบ shortcut' -ForegroundColor Cyan
    $ws = New-Object -ComObject WScript.Shell
    $removed = 0
    foreach ($dir in @([Environment]::GetFolderPath('CommonDesktopDirectory'), [Environment]::GetFolderPath('CommonPrograms'))) {
        # any name: only shortcuts that open this folder's GUI (setup.ps1 -ShortcutName may have renamed them)
        foreach ($f in @(Get-ChildItem -Path $dir -Filter '*.lnk' -File -ErrorAction SilentlyContinue)) {
            $lnkArgs = $ws.CreateShortcut($f.FullName).Arguments
            if ($lnkArgs -match 'orch_gui\.py' -and (Refers-ToInstall $lnkArgs)) {
                Remove-Item -LiteralPath $f.FullName
                Ok "ลบ $($f.FullName)"
                $removed++
            }
        }
    }
    if (-not $removed) { Ok 'ไม่มี shortcut ของโฟลเดอร์นี้' }

    Write-Host ''
    Write-Host '==> เอาโฟลเดอร์ออกจาก PATH ของเครื่อง' -ForegroundColor Cyan
    $raw = (Get-Item $EnvKey).GetValue('Path', '', 'DoNotExpandEnvironmentNames')
    $parts = @($raw -split ';' | Where-Object { $_ })
    $keep = @($parts | Where-Object { $_.TrimEnd('\') -ine $InstallDir })
    if ($keep.Count -ne $parts.Count) {
        Set-ItemProperty -Path $EnvKey -Name Path -Value ($keep -join ';') -Type ExpandString
        Send-EnvChange
        Ok "เอา $InstallDir ออกแล้ว (Python ยังอยู่ใน PATH เพราะโปรแกรมอื่นอาจใช้)"
    } else {
        Ok 'ไม่มีใน PATH'
    }

    Write-Host ''
    Write-Host '==================== ถอนการติดตั้งเสร็จแล้ว ====================' -ForegroundColor Green
    Write-Host "  ข้อมูลยังอยู่ที่ $InstallDir (orch.db = งานและประวัติการรัน, logs)"
    Write-Host '  ติดตั้งกลับด้วย setup.bat จะได้งานเดิมกลับมาทั้งหมด'
    Write-Host '  ถ้าไม่ต้องการข้อมูลแล้ว ให้ลบโฟลเดอร์นี้เอง'
    Write-Host "  log การถอนการติดตั้ง: $LogFile"
} catch {
    Write-Host ''
    Write-Host "ถอนการติดตั้งไม่สำเร็จ: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  ดูรายละเอียดใน $LogFile"
    $rc = 1
} finally {
    Stop-Transcript | Out-Null
}
exit $rc
