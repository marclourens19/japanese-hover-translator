$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
# -t . matters: without it, unittest treats tests/ itself as the top-level
# directory and never runs tests/__init__.py (which puts src/ on sys.path)
# before importing test modules -- every `import hover_translate` would fail.
python -m unittest discover -s tests -t . -v
if ($LASTEXITCODE -ne 0) {
    throw "Test suite failed with exit code $LASTEXITCODE"
}
