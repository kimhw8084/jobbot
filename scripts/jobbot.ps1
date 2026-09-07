$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Run: py -m venv .venv; .venv\Scripts\python -m pip install -e ." }
& $Python -m jobbot @args
exit $LASTEXITCODE
