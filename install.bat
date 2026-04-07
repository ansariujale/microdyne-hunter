@echo off
title MicrodyneHunter v2 — Installer
color 0A

echo.
echo  ======================================================
echo   MicrodyneHunter v2 — One-Click Installer
echo  ======================================================
echo.

echo  [1/4] Checking Python...
python --version 2>nul
if %errorlevel% neq 0 (
    echo  ERROR: Python not found. Install from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo.
echo  [2/4] Installing Python packages...
pip install anthropic httpx python-dotenv playwright beautifulsoup4 lxml tenacity schedule rich pydantic

echo.
echo  [3/4] Installing Chromium browser for Playwright...
python -m playwright install chromium

echo.
echo  [4/4] Setting up .env file...
if not exist ".env" (
    copy .env.example .env
    echo  Created .env — edit it with your API keys!
) else (
    echo  .env already exists
)

echo.
echo  ======================================================
echo   DONE! Now run:  python main.py --stats
echo  ======================================================
pause
