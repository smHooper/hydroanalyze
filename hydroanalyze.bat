@echo off
setlocal

set "PROJECT_NAME=hydroanalyze"
set "PACKAGE_DIR=%~dp0%PROJECT_NAME%"
set "ENV=%HYDROANALYZE_ENV%"

if not defined ENV (
    echo ERROR: HYDROANALYZE_ENV environment variable is not defined.
    exit /b 1
)

if not exist "%ENV%\python.exe" (
    echo ERROR: Python executable not found: %ENV%\python.exe
    exit /b 1
)

if not exist "%PACKAGE_DIR%\cli.py" (
    echo ERROR: CLI script not found: %PACKAGE_DIR%\cli.py
    exit /b 1
)

set "PYTHONDONTWRITEBYTECODE=1"
set "PATH=%ENV%;%ENV%\Library\bin;%ENV%\Scripts;%PACKAGE_DIR%;%PACKAGE_DIR%\%PROJECT_NAME%;%PATH%"

"%ENV%\python.exe" %PACKAGE_DIR%\cli.py %*

exit /b %ERRORLEVEL%
