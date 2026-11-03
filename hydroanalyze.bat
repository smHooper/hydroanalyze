@echo off
setlocal

set "PROJECT_NAME=hydroanalyze"
set "PACKAGE_DIR=%~dp0%PROJECT_NAME%"
set "ENV=%HYDROANALYZE_ENV%"
set "PYTHONDONTWRITEBYTECODE=1"
set "PATH=%ENV%;%ENV%\Library\bin;%ENV%\Scripts;%PACKAGE_DIR%;%PACKAGE_DIR%\%PROJECT_NAME%;%PATH%"
"%ENV%\python.exe" %PACKAGE_DIR%\cli.py %*