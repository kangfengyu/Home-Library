@echo off
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo [First run] Creating venv .venv ...
  python -m venv .venv
)
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -c "import fastapi" 2>nul || (
  echo Installing dependencies ...
  "%PY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Retry with Tsinghua mirror ...
    "%PY%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  )
)
echo Starting Home Library: http://localhost:5000
"%PY%" app\main.py
pause
