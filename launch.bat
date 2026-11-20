@echo off

cd /d "%~dp0"

echo ========================================
echo Rain Map Launcher
echo ========================================
echo.

REM Check for virtual environment
if not exist ".venv" (
    echo Virtual environment not found.
    echo Running build.bat...
    call build.bat

    if errorlevel 1 (
        echo Build failed.
        pause
        exit /b 1
    )
)


REM Check for Python inside the venv
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Virtual environment appears broken.
    echo.
    echo Run build.bat again.
    echo.
    pause
    exit /b 1
)

REM Activate venv
call .venv\Scripts\activate

REM Verify app exists
if not exist "app.py" (
    echo ERROR: app.py not found.
    echo.
    pause
    exit /b 1
)

echo Starting Rain Map...
echo.

REM Open browser after a short delay so Dash has time to start
start "" powershell -WindowStyle Hidden -Command "Start-Sleep 3; Start-Process 'http://127.0.0.1:8050'"

python app.py

echo.
echo Rain Map has stopped.
pause