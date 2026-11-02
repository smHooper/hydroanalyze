@echo off
set "PACKAGE_DIR=%~dp0"
set "ENV=C:\ProgramData\miniconda3\envs\hydroanalyze"
set "PYTHONDONTWRITEBYTECODE=1"
set "PATH=%ENV%;%ENV%\Library\bin;%ENV%\Scripts;%PACKAGE_DIR%;%PATH%"
"%ENV%\python.exe" %PACKAGE_DIR%cli.py %*