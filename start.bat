@echo off
setlocal
REM --------------------------------------------------------------------------
REM  Development launcher.
REM
REM  This script never creates a default account and never invents a password:
REM  the audited revision printed "Default login: admin / admin123", which was
REM  not true and encouraged a guessable administrator account.  Create your
REM  first user explicitly with the command below.
REM --------------------------------------------------------------------------
cd /d "%~dp0"

echo Starting the building-materials management system...
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not on PATH. Install Python 3.11+ first.
    pause
    exit /b 1
)

if not exist ".venv" (
    echo Creating virtual environment .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: could not create the virtual environment.
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"

echo Installing / updating dependencies ...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: dependency installation failed.
    pause
    exit /b 1
)

if not exist ".env" (
    if exist ".env.example" (
        echo Creating .env from .env.example - review it before production use.
        copy /y ".env.example" ".env" >nul
    )
)

REM Create the schema and reference data.  This is idempotent and never drops tables.
echo.
echo Creating the schema and reference data (safe to re-run) ...
python -m flask --app wsgi init-db
if errorlevel 1 (
    echo ERROR: database initialisation failed. Check DATABASE_URL / SECRET_KEY.
    pause
    exit /b 1
)

echo.
echo --------------------------------------------------------------------------
echo  If you have not created an administrator yet, run this in another window:
echo.
echo      .venv\Scripts\flask --app wsgi create-user --username admin --role admin
echo.
echo  You will be prompted for the password; it is never stored in this script.
echo --------------------------------------------------------------------------
echo.
echo The interface will be available at: http://127.0.0.1:5000
echo Press Ctrl+C to stop.
echo.

python app.py

pause
endlocal
