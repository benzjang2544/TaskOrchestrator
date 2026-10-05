@echo off
rem Open the Orch GUI (no console window)
cd /d "%~dp0"
where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw "%~dp0orch_gui.py" %*
) else (
    start "Orch GUI" python "%~dp0orch_gui.py" %*
)
