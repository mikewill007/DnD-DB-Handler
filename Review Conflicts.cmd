@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" review_conflicts.py %*
) else (
    py -3 review_conflicts.py %*
)
if errorlevel 1 pause
