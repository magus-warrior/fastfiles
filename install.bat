@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "fastfiles_exit=%errorlevel%"
echo.
if not "%fastfiles_exit%"=="0" (
    echo FastFiles setup failed. See the error above.
) else (
    echo Setup complete. Open FastFiles from the Start menu or double-click start.bat.
)
pause
exit /b %fastfiles_exit%
