@echo off
rem Run the engine of one queue in this window:  orch_engine.bat [QUEUE]   (Ctrl+C to stop)
cd /d "%~dp0"
set "ORCH_QUEUE=%~1"
if "%ORCH_QUEUE%"=="" set "ORCH_QUEUE=default"
title Orch engine - %ORCH_QUEUE%
python "%~dp0orch.py" engine --queue "%ORCH_QUEUE%"
echo.
echo Engine stopped (exit code %errorlevel%).
pause
