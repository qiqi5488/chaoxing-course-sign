@echo off
cd /d "%~dp0"
python -m uvicorn server:create_app --factory --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
pause
