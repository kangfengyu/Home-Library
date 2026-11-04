@echo off
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo [First run] Creating venv .venv ...
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo Starting Home Library: http://localhost:5000
"%PY%" app\main.py
pause
