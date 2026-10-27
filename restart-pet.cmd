@echo off
rem dsh-pet-pure restarter: kills all instances, starts fresh (latest code).
rem Same as start-pet.cmd but with the kill step; use after config/code changes.
taskkill /IM pythonw.exe /F >nul 2>&1
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" main.py
