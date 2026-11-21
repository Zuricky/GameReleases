@echo off
chcp 65001 >nul
cd /d "%~dp0"
title GameReleasesBot

if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] The .venv folder was not found. Create it with:
    echo     python -m venv .venv
    echo     .venv\Scripts\python -m pip install -e .
    echo.
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"
python -m gamereleases
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] The bot exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
