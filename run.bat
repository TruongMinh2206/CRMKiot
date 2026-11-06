@echo off
cd /d %~dp0
where python >nul 2>nul || (echo Chua cai Python. Tai tai https://www.python.org/downloads/ va tick "Add Python to PATH" & pause & exit /b)
if not exist .venv (python -m venv .venv)
call .venv\Scripts\activate
pip install -q -r requirements.txt
if not exist .env copy .env.example .env
start http://localhost:8000
uvicorn app.main:app --host 0.0.0.0 --port 8000
pause
