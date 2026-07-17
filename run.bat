@echo off
cd /d "%~dp0"
set PYTHONUTF8=1

if not exist .venv\Scripts\python.exe (
    echo [setup] Creating virtual environment...
    python -m venv .venv || goto :error
    echo [setup] Installing dependencies...
    .venv\Scripts\python.exe -m pip install -q -r requirements.txt || goto :error
)

if not exist .env (
    echo [setup] No .env file - creating from template, opening Notepad.
    echo [setup] Fill it in, save, close Notepad - then the bot will start.
    copy .env.example .env >nul
    start /wait notepad .env
)

echo [run] Starting bot. Stop: Ctrl+C or close this window.
.venv\Scripts\python.exe bot.py
goto :end

:error
echo Something went wrong - see messages above.

:end
pause
