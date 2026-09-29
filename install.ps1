[CmdletBinding()]
param([switch]$Headless)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $Python)) {
        if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv .venv }
        elseif (Get-Command python -ErrorAction SilentlyContinue) { & python -m venv .venv }
        else { throw 'Install Python 3.10 or newer, then run install.ps1 again.' }
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
    }
    & $Python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer is required.' }
    $Package = if ($Headless) { '.' } else { '.[desktop]' }
    & $Python -m pip install -e $Package
    if ($LASTEXITCODE -ne 0) { throw 'Could not install FastFiles.' }
    & $Python -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency validation failed.' }
    if (-not $Headless) {
        & $Python -c 'from PySide6.QtWidgets import QApplication; import qt_material; app = QApplication([])'
        if ($LASTEXITCODE -ne 0) { throw 'Qt desktop startup failed.' }
    }
    & $Python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'FastFiles tests failed.' }
    if (-not $Headless) {
        $Programs = [Environment]::GetFolderPath('Programs')
        $Shell = New-Object -ComObject WScript.Shell
        $Shortcut = $Shell.CreateShortcut((Join-Path $Programs 'FastFiles.lnk'))
        $Shortcut.TargetPath = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
        $Shortcut.Arguments = '-m fastfiles'
        $Shortcut.WorkingDirectory = $PSScriptRoot
        $Shortcut.Description = 'Send and receive files over your network'
        $Shortcut.Save()
    }
    Write-Host 'FastFiles installed. Open FastFiles from the Start menu or double-click start.bat.'
    Write-Host 'For a headless Locker, run .\start.ps1 --serve.'
    Write-Host 'Direct SSH additionally requires Cygwin rsync, openssh, and cygpath on PATH; see README.md.'
} finally {
    Pop-Location
}
