@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not defined PORT set "PORT=8001"
set "URL=http://127.0.0.1:%PORT%"
set "PYTHONIOENCODING=utf-8"
if not defined LOG_LEVEL set "LOG_LEVEL=INFO"
if not defined LOG_STREAM set "LOG_STREAM=1"

if not exist "%PYTHON%" (
  echo [ReActron] Missing .venv Python: "%PYTHON%"
  echo [ReActron] Create the repository .venv from requirements.lock first.
  exit /b 1
)

netstat -ano | findstr "LISTENING" | findstr "127.0.0.1:%PORT%" >nul
if not errorlevel 1 (
  echo [ReActron] Service already running: %URL%
  exit /b 0
)

echo [ReActron] Starting %URL%
"%PYTHON%" -m uvicorn backend.app:app --host 127.0.0.1 --port "%PORT%" --workers 1
if errorlevel 1 (
  echo [ReActron] Startup failed, see errors above.
  exit /b 1
)

endlocal
