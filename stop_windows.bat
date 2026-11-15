@echo off
rem Stops the dividend dashboard. Optional: pass the port if you started it with --port.
set PORT=%1
if "%PORT%"=="" set PORT=8765
curl.exe -s -f -X POST -H "X-Dashboard-Stop: 1" http://127.0.0.1:%PORT%/api/shutdown >nul 2>&1
if not errorlevel 1 (
  echo Dashboard stopped.
  timeout /t 2 >nul
  exit /b 0
)
rem Server didn't answer: end whatever process is listening on the port.
set FOUND=
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:"127\.0\.0\.1:%PORT% .*LISTENING"') do (
  taskkill /PID %%p /F >nul 2>&1
  set FOUND=1
)
if defined FOUND (echo Dashboard stopped.) else (echo The dashboard is not running on port %PORT%.)
timeout /t 3 >nul
