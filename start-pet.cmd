@echo off
rem dsh-pet-pure launcher: double-click to bring the pet up.
rem Registry entry auto-published (pet-http.d/pure.json), driver claims it.
rem Logs: %USERPROFILE%\.dsh\pet-pure.log | Config: pet-pure.json
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" main.py
