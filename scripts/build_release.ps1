$ErrorActionPreference = "Stop"

# This script lives in scripts/, one level below the project root -- run
# everything from the root so requirements.txt/dist/build resolve the same
# way they would if you ran these commands by hand from the repo root.
Set-Location (Split-Path $PSScriptRoot -Parent)
python -m pip install -r requirements.txt -r requirements-build.txt
python -m PyInstaller --noconfirm --clean scripts\japanese_hover_translator.spec

Write-Host ""
Write-Host "Release ready in dist\JapaneseHoverTranslator"
