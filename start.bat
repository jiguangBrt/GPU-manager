@echo off
cd /d "%~dp0"
uv run --locked --no-dev python -E run.py %*
if errorlevel 1 pause
