@echo off

echo ========================================
echo Building Rain Map environment
echo ========================================

set PY=

py -3 --version >nul 2>&1 && set PY=py -3

if not defined PY (
    python --version >nul 2>&1 && set PY=python
)

if not defined PY (
    echo Python was not found.
    pause
    exit /b 1
)

if exist .venv\Scripts\pip.exe (
    .venv\Scripts\pip.exe --version >nul 2>&1 || (
        echo Existing venv is invalid. Recreating...
        rmdir /s /q .venv
    )
)

if not exist .venv\Scripts\python.exe (
    %PY% -m venv .venv || goto :fail
)

call .venv\Scripts\activate || goto :fail

python -m pip install --upgrade pip

python -m pip install ^
    dash ^
    dash-leaflet ^
    geopandas ^
    pandas ^
    shapely ^
    pyogrio

echo.
echo Environment ready.
echo.
echo To start the app run:
echo     launch.bat
echo.

pause
exit /b 0

:fail
echo.
echo Build failed.
pause
exit /b 1