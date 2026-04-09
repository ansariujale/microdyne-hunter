@echo off
title FlowLock Overseas — Command Center
color 0B

echo.
echo  ======================================================
echo   FlowLock Overseas — Starting Command Center...
echo  ======================================================
echo.

:: Check Python
python --version 2>nul
if %errorlevel% neq 0 (
    echo  ERROR: Python not found. Install from https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Check if key packages are installed
python -c "import httpx, anthropic, rich" 2>nul
if %errorlevel% neq 0 (
    echo  Some packages missing. Running quick install...
    pip install anthropic httpx python-dotenv playwright beautifulsoup4 lxml tenacity schedule rich pydantic
    echo.
)

:: Check .env
if not exist ".env" (
    if exist ".env.example" (
        copy .env.example .env
        echo  Created .env from template — edit it with your API keys!
        echo.
    )
)

echo  Starting server at http://localhost:8001 ...
echo  Dashboard will open in your browser automatically.
echo  Press Ctrl+C to stop.
echo.

set PORT=8001
python server.py

pause
