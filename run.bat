@echo off
chcp 65001 >nul
title Lecture Transcriber (close this window to stop)
cd /d "%~dp0"

set "VENV=%~dp0.venv"
if not exist "%VENV%\Scripts\activate.bat" (
  echo [ERROR] .venv not found: %VENV%
  echo Run: py -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 1
)
call "%VENV%\Scripts\activate.bat"

python -c "import fastapi, uvicorn, multipart" 2>nul || (
  echo Installing web packages...
  python -m pip install -r requirements.txt || (pause & exit /b 1)
)

rem server.py opens http://127.0.0.1:8765 in the browser when it is ready
python server.py
if errorlevel 1 pause
