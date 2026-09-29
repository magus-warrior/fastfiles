[CmdletBinding()]
param([switch]$Headless)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw 'Install Git for Windows before updating FastFiles.'
    }
    if (-not (Test-Path '.git')) {
        throw 'This folder is not a Git checkout. Clone FastFiles to use the updater.'
    }
    $Changes = & git status --porcelain
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the Git checkout.' }
    if ($Changes) { throw 'Save your local changes in Git or move them aside before updating.' }
    & git symbolic-ref --quiet HEAD | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Check out a branch before updating.' }
    & git rev-parse --verify '@{upstream}' 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'This branch needs a Git remote tracking branch before updating.' }
    Write-Host 'Downloading FastFiles updates...'
    & git -c merge.autostash=false -c rebase.autostash=false pull --ff-only --no-rebase
    if ($LASTEXITCODE -ne 0) {
        throw 'Git update failed. Check your network and branch history; installation was not run.'
    }
    $InstallArguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        (Join-Path $PSScriptRoot 'install.ps1'))
    if ($Headless) { $InstallArguments += '-Headless' }
    & powershell.exe @InstallArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'Files were updated, but installation failed. Fix the error and rerun install.bat.'
    }
    Write-Host 'FastFiles updated. Restart the app to use the new version.'
} finally {
    Pop-Location
}
