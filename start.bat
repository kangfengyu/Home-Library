@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo [首次运行] 正在创建虚拟环境 .venv ...
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo 正在启动书房（本地书库）: http://localhost:5000
"%PY%" app\main.py
