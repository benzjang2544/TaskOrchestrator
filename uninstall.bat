@echo off
rem Orch uninstall: remove the engine task(s), shortcuts and PATH entry of this folder. Keeps orch.db and logs.
rem Options: uninstall.bat [-InstallDir C:\Orch]
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
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
set "RC=%errorlevel%"
echo.
pause
exit /b %RC%
