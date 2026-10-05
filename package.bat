@echo off
rem Build dist\Orch-YYYYMMDD.zip from the committed files (needs git). Ship the zip; the user extracts it and runs setup.bat.
rem Offline machines: put python-3.12.x-amd64.exe and pyodbc-*.whl next to setup.bat after extracting.
setlocal
cd /d "%~dp0"
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "STAMP=%%d"
if not exist dist mkdir dist
git archive --format=zip --prefix=Orch/ -o "dist\Orch-%STAMP%.zip" HEAD
if errorlevel 1 goto failed
git status --porcelain | findstr . >nul
if not errorlevel 1 echo WARNING: uncommitted changes are NOT in the zip (commit first).
echo Created dist\Orch-%STAMP%.zip
exit /b 0
:failed
echo git archive failed (is git installed and is this a git repository?)
exit /b 1
