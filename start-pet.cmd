@echo off
rem dsh-pet-pure 启动器：双击即上桌（注册表自动发布，driver 自动认领）
rem 日志/配置：~/.dsh/pet-pure.log 与 ~/.dsh/pet-pure.json
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" main.py
