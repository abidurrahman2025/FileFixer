@echo off
REM Builds FileFixer.exe (one double-clickable file) in the "dist" folder.
REM Needs Python from python.org (with "tcl/tk" - the default installer includes it).
cd /d "%~dp0"
py -m pip install --upgrade pyinstaller
py -m PyInstaller --onefile --windowed --name FileFixer app.py
echo.
echo Done. Your program is: %~dp0dist\FileFixer.exe
pause
