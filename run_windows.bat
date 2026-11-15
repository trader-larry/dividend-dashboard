@echo off
cd /d "%~dp0"
python server.py %*
rem Keep the window open only if the server ended with an error, so you can read it.
if errorlevel 1 pause
