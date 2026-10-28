@echo off
rem dsh-pet-pure restarter: kills all instances, clears any stale exit flag,
rem starts fresh (latest code). Use after config/code changes.
taskkill /IM pythonw.exe /F >nul 2>&1
if exist "%USERPROFILE%\.dsh\pet-pure-exit.flag" del "%USERPROFILE%\.dsh\pet-pure-exit.flag"
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" main.py
