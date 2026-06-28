@echo off
setlocal

rem ===== Social Platform Scraper - Launcher =====
cd /d "%~dp0"

set "PY=%~dp0venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [ERROR] Python venv not found: %PY%
    echo Please create the virtual environment and install deps first:
    echo     python -m venv venv
    echo     venv\Scripts\pip.exe install -r requirements.txt
    echo     venv\Scripts\python.exe -m playwright install chromium
    echo.
    pause
    exit /b 1
)

if not exist "%~dp0main.py" (
    echo [ERROR] main.py not found. Place this BAT in the project root.
    pause
    exit /b 1
)

echo Starting Social Platform Scraper ...
"%PY%" main.py
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
    echo.
    echo [INFO] Exit code: %EXITCODE%
    pause
)

endlocal
exit /b %EXITCODE%
