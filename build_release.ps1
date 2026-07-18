$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
python -m pip install -r requirements.txt -r requirements-build.txt
python -m PyInstaller --noconfirm --clean japanese_hover_translator.spec

Write-Host ""
Write-Host "Release ready in dist\JapaneseHoverTranslator"
