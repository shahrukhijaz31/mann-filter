@echo off
cd /d "%~dp0"
start "" "venv\Scripts\pythonw.exe" scraper_pro.py
if %errorlevel% neq 0 (
    echo Error occurred. Trying with console...
    "venv\Scripts\python.exe" scraper_pro.py
    pause
)
