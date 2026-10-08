@echo off
rem Orch setup for the UI automation queue: same as "setup.bat -Queue ui -Trigger logon".
rem Sign in as the robot user first, then double-click; any admin account can answer the UAC prompt.
rem The engine task belongs to the user signed in now (add -User DOMAIN\user to choose another).
call "%~dp0setup.bat" -Queue ui -Trigger logon %*
exit /b %errorlevel%
