@echo off
title Microdyne Engineering — Agent Runner
color 0B
set PORT=8001

echo.
echo  ======================================================
echo   MicrodyneHunter v2 — Agent Runner
echo  ======================================================
echo.
echo  Select what to run:
echo.
echo  [1] Full Daily Pipeline (scrape + qualify + email + forms + followup)
echo  [2] Scrape Only (find new leads)
echo  [3] Send Emails Only
echo  [4] Fill Forms Only
echo  [5] Send Follow-ups Only
echo  [6] Generate Intelligence Report
echo  [7] Show Stats
echo  [8] Verify Setup
echo  [9] Open Dashboard
echo  [0] Exit
echo.
set /p choice="  Enter choice (0-9): "

if "%choice%"=="1" (
    echo.
    echo  Running full pipeline...
    python main.py
)
if "%choice%"=="2" (
    echo.
    echo  Running scraper...
    python main.py --scrape
)
if "%choice%"=="3" (
    echo.
    echo  Sending emails...
    python main.py --email
)
if "%choice%"=="4" (
    echo.
    echo  Filling forms...
    python main.py --forms
)
if "%choice%"=="5" (
    echo.
    echo  Sending follow-ups...
    python main.py --followup
)
if "%choice%"=="6" (
    echo.
    echo  Generating report...
    python main.py --report
)
if "%choice%"=="7" (
    echo.
    echo  Fetching stats...
    python main.py --stats
)
if "%choice%"=="8" (
    echo.
    python verify_setup.py
)
if "%choice%"=="9" (
    start dashboard.html
    goto end
)
if "%choice%"=="0" (
    goto end
)

echo.
echo  Done! Press any key to return to menu...
pause >nul
call run.bat

:end
