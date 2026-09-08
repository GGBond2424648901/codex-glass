@echo off
setlocal
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
    echo Activate your existing Conda environment, install requirements-desktop.txt,
    echo then run: python desktop_widget.py
    pause
    exit /b 1
)
start "" pythonw "%~dp0desktop_widget.py"
