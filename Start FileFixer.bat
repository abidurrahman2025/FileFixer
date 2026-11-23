@echo off
REM Double-click to start FileFixer without a black console window.
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw app.py & exit /b)
where pythonw >nul 2>nul && (start "" pythonw app.py & exit /b)
echo Python was not found. Install it from python.org, then try again.
pause
