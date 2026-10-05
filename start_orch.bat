@echo off
rem Start the engine of queue "default" in its own window, then open the GUI
cd /d "%~dp0"
start "Orch engine - default" "%~dp0orch_engine.bat" default
call "%~dp0orch_gui.bat"
