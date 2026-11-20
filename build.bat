@echo off
REM Build rain_map.exe (run on Windows, from this folder).
REM Python is needed on THIS machine only to build; the exe runs without it.

set PY=
py -3 --version >nul 2>&1 && set PY=py -3
if not defined PY (
    python --version >nul 2>&1 && set PY=python
)
if not defined PY (
    echo Python was not found on this machine.
    echo Install it once with:  winget install Python.Python.3.12
    echo or from https://www.python.org/downloads/  ^(check "Add python.exe to PATH"^)
    echo Then close this window, and run build.bat again.
    pause
    exit /b 1
)

REM If the venv was copied or moved, its launchers point at the old path. Rebuild it.
if exist .venv\Scripts\pip.exe (
    .venv\Scripts\pip.exe --version >nul 2>&1 || (
        echo Existing .venv is broken ^(folder was probably moved or copied^). Recreating...
        rmdir /s /q .venv
    )
)

if not exist .venv\Scripts\python.exe (
    %PY% -m venv .venv || goto :fail
)
call .venv\Scripts\activate || goto :fail
python -m pip install dash folium geopandas pyinstaller || goto :fail
python -m PyInstaller --onefile --noconfirm --clean --name rain_map ^
    --add-data "data;data" ^
    --collect-all dash ^
    --collect-data folium ^
    --collect-data branca ^
    --collect-all pyogrio ^
    --collect-all shapely ^
    app.py || goto :fail

echo.
echo Done. Your exe is at dist\rain_map.exe
pause
exit /b 0

:fail
echo.
echo Build failed. See the messages above.
pause
exit /b 1