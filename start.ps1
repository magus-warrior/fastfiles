$Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    Write-Error 'FastFiles is not installed. Run .\install.ps1 first.'
    exit 1
}
& $Python -m fastfiles @args
exit $LASTEXITCODE
