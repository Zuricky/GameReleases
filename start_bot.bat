@echo off
chcp 65001 >nul
cd /d "%~dp0"
title GameReleasesBot - by Zuricky
color 0B

echo ===================================================
echo      🎮 GameReleasesBot - by Zuricky
echo ===================================================
echo.

echo [1/3] Verifica ambiente virtuale...
if not exist ".venv\Scripts\python.exe" (
    echo [ERRORE] La cartella .venv non esiste in questo percorso!
    echo Creala aprendo il terminale qui e scrivendo:
    echo     python -m venv .venv
    echo     .venv\Scripts\python -m pip install -e .
    echo.
    pause
    exit /b
)

echo [2/3] Avvio bot Discord...
".venv\Scripts\python.exe" -m gamereleases

echo.
echo [3/3] Bot terminato. Premi un tasto per chiudere.
pause >nul
