@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" %*
set "fastfiles_exit=%errorlevel%"
echo.
if not "%fastfiles_exit%"=="0" echo FastFiles update failed. See the error above.
pause
exit /b %fastfiles_exit%
