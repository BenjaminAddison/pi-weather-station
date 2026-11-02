@echo off
rem WeatherPi Companion dev launcher: double-click, pick a part, the app opens.
rem Runs the app from source with the repo's .venv (see docs/dev-environment.md).
rem It always talks to the real Pi on the USB cable (there's no pretend Pi any more).
setlocal EnableExtensions
title WeatherPi Companion (dev launcher)
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Can't find the Python environment at:
    echo   %PY%
    echo Set it up first, see docs\dev-environment.md.
    pause
    exit /b 1
)

:ask_part
echo.
echo  WeatherPi Companion - which part?
echo   1. The whole app   [default]
echo   2. Flash SD card only
echo   3. Check the Pi only
echo   4. Set up only
echo.
set "PART=1"
set /p "PART=Part [1-4, Enter = 1]: "
set "PART=%PART: =%"
set "ONLY="
if "%PART%"=="1" goto part_ok
if "%PART%"=="2" set "ONLY=--only flasher" & goto part_ok
if "%PART%"=="3" set "ONLY=--only tester" & goto part_ok
if "%PART%"=="4" set "ONLY=--only setup" & goto part_ok
echo Please type 1, 2, 3 or 4.
goto ask_part
:part_ok

set "PYTHONPATH=%~dp0app"
set "WEATHERPI_COMPANION_HOME="
rem Ctrl+Shift+S skips a wizard page whose checks won't pass (dev only)
set "WEATHERPI_DEV_SKIP=1"

echo.
echo The app uses your real app data (%%LOCALAPPDATA%%\WeatherPi)
echo and talks to the Pi at 10.12.194.1. "Set up" claims the Pi for real.
set "GO=n"
set /p "GO=Continue? [y/N]: "
if /i not "%GO%"=="y" exit /b 0

echo.
echo Starting WeatherPi Companion... (close its window to finish)
"%PY%" -m weatherpi_companion %ONLY%
set "RC=%errorlevel%"
if not "%RC%"=="0" (
    echo.
    echo The app exited with an error ^(code %RC%^). See the messages above.
    pause
)
exit /b %RC%
