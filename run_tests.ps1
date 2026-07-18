$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) {
    throw "Test suite failed with exit code $LASTEXITCODE"
}
