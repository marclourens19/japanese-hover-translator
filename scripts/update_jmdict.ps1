$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $PSScriptRoot
$temporary = Join-Path ([System.IO.Path]::GetTempPath()) ("jmdict-update-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temporary | Out-Null

try {
    $headers = @{
        Accept = "application/vnd.github+json"
        "User-Agent" = "JapaneseHoverTranslator-JMdict-Updater"
    }
    $release = Invoke-RestMethod `
        -Headers $headers `
        -Uri "https://api.github.com/repos/scriptin/jmdict-simplified/releases/latest"
    $asset = $release.assets | Where-Object {
        $_.name -match '^jmdict-eng-[^/]+\.json\.zip$'
    } | Select-Object -First 1
    if ($null -eq $asset) {
        throw "The latest release does not contain an English JMdict JSON ZIP."
    }

    $archive = Join-Path $temporary $asset.name
    Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -OutFile $archive
    Expand-Archive -LiteralPath $archive -DestinationPath $temporary
    $json = Get-ChildItem -Path $temporary -Filter "jmdict-eng-*.json" | Select-Object -First 1
    if ($null -eq $json) {
        throw "The downloaded archive did not contain the expected JSON file."
    }

    python (Join-Path $PSScriptRoot "build_jmdict.py") `
        $json.FullName `
        (Join-Path $project "data\jmdict_english.sqlite3")
}
finally {
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Recurse -Force
    }
}
