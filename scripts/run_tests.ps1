$ErrorActionPreference = "Stop"

# This script lives in scripts/, one level below the project root -- go up
# one level so -s tests/-t . below resolve against the real tests/ folder.
Set-Location (Split-Path $PSScriptRoot -Parent)
# -t . matters: without it, unittest treats tests/ itself as the top-level
# directory and never runs tests/__init__.py (which puts src/ on sys.path)
# before importing test modules -- every `import hover_translate` would fail.
python -m unittest discover -s tests -t . -v
if ($LASTEXITCODE -ne 0) {
    throw "Test suite failed with exit code $LASTEXITCODE"
}
