@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
  echo The project environment is missing.
  echo.
  echo Run setup.ps1 once, then double-click this launcher again.
  echo.
  pause
  exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "%~dp0launcher.pyw"
exit /b 0

