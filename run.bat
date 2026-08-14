@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=E:\anaconda\envs\countbot\python.exe"
set "PORT=8001"
set "URL=http://127.0.0.1:%PORT%"
set "PYTHONIOENCODING=utf-8"
set "LOG_LEVEL=INFO"
set "LOG_STREAM=1"

if not exist "%PYTHON%" (
  echo [countboter] Python not found: %PYTHON%
  pause
  exit /b 1
)

netstat -ano | findstr "LISTENING" | findstr "127.0.0.1:%PORT%" >nul
if not errorlevel 1 (
  echo [countboter] Service already running: %URL%
  pause
  exit /b 0
)

echo [countboter] Starting %URL%
"%PYTHON%" -m uvicorn backend.app:app --host 127.0.0.1 --port %PORT%
if errorlevel 1 (
  echo [countboter] Startup failed, see errors above.
  pause
)

endlocal
