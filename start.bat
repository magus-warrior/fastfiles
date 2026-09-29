@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Setting up FastFiles for the first time...
    call "%~dp0install.bat"
    if errorlevel 1 exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m fastfiles %*
set "fastfiles_exit=%errorlevel%"
if not "%fastfiles_exit%"=="0" (
    echo.
    echo FastFiles could not start. See the error above.
    echo Try double-clicking install.bat to repair the installation.
    pause
)
exit /b %fastfiles_exit%
