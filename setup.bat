@echo off
rem Orch setup: install or update Orch on this machine. Asks for Administrator rights (UAC).
rem Options: setup.bat [-InstallDir C:\Orch] [-Queue default] [-Trigger boot or logon]
setlocal
fltmc >nul 2>&1
if not errorlevel 1 goto elevated
echo Requesting Administrator rights...
if "%~1"=="" goto elevate_noargs
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
exit /b
:elevate_noargs
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b
:elevated
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set "RC=%errorlevel%"
echo.
pause
exit /b %RC%
