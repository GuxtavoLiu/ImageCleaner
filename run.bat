@echo off
REM Abre o ImageCleaner sem janela de console (log em %LOCALAPPDATA%\ImageCleaner)
cd /d "%~dp0"
start "" pythonw "%~dp0main.py"
