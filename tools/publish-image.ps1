# Attach a WeatherPi SD card image to a GitHub release (the release workflow builds only the
# installer; the image needs the privileged builder, D17). See docs/companion-app.md
# "Auto-build releases on GitHub".
#
#   powershell -ExecutionPolicy Bypass -File tools\publish-image.ps1 -Tag v0.2.0 `
#       -Image ..\images\weatherpi-2026.11.02-2.img.xz [-Publish]
#
# Checks the image against its .sha256 first, then uploads both (replacing any already there).
# -Publish also takes the release out of draft, which makes it the one the app's flasher and
# owners download (releases/latest). Needs the GitHub CLI, signed in: gh auth login

param(
    [Parameter(Mandatory = $true)][string]$Tag,
    [Parameter(Mandatory = $true)][string]$Image,
    [switch]$Publish
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Check-Exit($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }

$Image = (Resolve-Path $Image).Path
$name = Split-Path $Image -Leaf
if ($name -notmatch '^weatherpi-.+\.img\.xz$') { throw "$name doesn't look like weatherpi-<version>.img.xz" }
$sha = "$Image.sha256"
if (-not (Test-Path $sha)) { throw "missing $sha (the app's flasher needs it next to the image)" }

$expected = ((Get-Content $sha -Raw).Trim() -split '\s+')[0].ToLower()
Write-Host "Checking $name against its .sha256 ..."
$actual = (Get-FileHash $Image -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $expected) { throw "sha256 mismatch: $actual, the .sha256 file says $expected" }

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI not found: winget install GitHub.cli, then gh auth login"
}
# gh finds the GitHub repo from the git checkout it runs in, so run from this repo
# whatever folder the script was started from (the image path is already resolved)
Set-Location (Split-Path $PSScriptRoot -Parent)
gh release view $Tag --json tagName,isDraft | Out-Null
Check-Exit "gh release view $Tag (push the tag first; the workflow creates the draft release)"

Write-Host "Uploading $name and its checksum to $Tag ..."
gh release upload $Tag $Image $sha --clobber
Check-Exit "gh release upload"

if ($Publish) {
    gh release edit $Tag --draft=false --latest
    Check-Exit "gh release edit"
    Write-Host "Published $Tag. Owners (and the app's flasher) now get it from releases/latest."
} else {
    Write-Host "Uploaded. Check the draft on GitHub, then publish it (or rerun with -Publish)."
}
