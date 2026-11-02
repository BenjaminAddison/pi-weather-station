# Build the WeatherPi Companion installer: vendor files -> PyInstaller -> checks -> Inno Setup.
#
#   powershell -ExecutionPolicy Bypass -File installer\build.ps1 [-Python <python.exe>] [-ExpectVersion 0.2.0]
#
# Output: dist\WeatherPi-Companion-Setup-<version>.exe and its .sha256.
# Needs: the app's dev dependencies (pip install -e "app[dev]") and Inno Setup 6.

param(
    # defaults to the active venv, then the repo's .venv, then "python" on PATH
    [string]$Python = "",
    # fail unless app/weatherpi_companion/__init__.py has this version (CI passes the tag)
    [string]$ExpectVersion = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Repo = Split-Path $PSScriptRoot -Parent
$Dist = Join-Path $Repo "dist"
$AppDir = Join-Path $Dist "WeatherPi Companion"
$AppExe = Join-Path $AppDir "WeatherPi Companion.exe"

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Check-Exit($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }

if (-not $Python) {
    $candidates = @()
    if ($env:VIRTUAL_ENV) { $candidates += Join-Path $env:VIRTUAL_ENV "Scripts\python.exe" }
    $candidates += Join-Path $Repo ".venv\Scripts\python.exe"
    $Python = ($candidates | Where-Object { Test-Path $_ } | Select-Object -First 1)
    if (-not $Python) { $Python = "python" }
}

$init = Get-Content (Join-Path $Repo "app\weatherpi_companion\__init__.py") -Raw
if ($init -notmatch '__version__ = "([^"]+)"') { throw "can't find __version__ in __init__.py" }
$Version = $Matches[1]
if ($ExpectVersion -and $ExpectVersion -ne $Version) {
    throw "the app says version $Version but the release is ${ExpectVersion}: bump __version__ in app\weatherpi_companion\__init__.py"
}
Write-Host "WeatherPi Companion $Version (Python: $Python)"

Step "Third-party installers"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "fetch-vendor.ps1")
Check-Exit "fetch-vendor.ps1"

Step "PyInstaller"
& $Python -m PyInstaller (Join-Path $PSScriptRoot "weatherpi-companion.spec") --noconfirm `
    --distpath $Dist --workpath (Join-Path $Repo "build")
Check-Exit "PyInstaller"

Step "Bundle check"
& $Python (Join-Path $PSScriptRoot "check_bundle.py") $AppDir
Check-Exit "check_bundle.py"

# A windowed exe doesn't make PowerShell wait, so use Start-Process -Wait for the exit code.
$p = Start-Process -FilePath $AppExe -ArgumentList "--selftest" -Wait -PassThru -NoNewWindow
if ($p.ExitCode -ne 0) { throw "the built app's --selftest failed (exit $($p.ExitCode))" }
Write-Host "frozen --selftest ok"

Step "Inno Setup"
$iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
if (-not $iscc) {
    $iscc = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) { throw "Inno Setup 6 not found: winget install JRSoftware.InnoSetup" }
& $iscc /Qp "/DAppVersion=$Version" (Join-Path $PSScriptRoot "weatherpi-companion.iss")
Check-Exit "Inno Setup"

$Setup = Join-Path $Dist "WeatherPi-Companion-Setup-$Version.exe"
$hash = (Get-FileHash $Setup -Algorithm SHA256).Hash.ToLower()
# same format as sha256sum, so `sha256sum -c` works on it
[IO.File]::WriteAllText("$Setup.sha256", "$hash *$(Split-Path $Setup -Leaf)`n")

Step "Done"
Write-Host "$Setup ($([math]::Round((Get-Item $Setup).Length / 1MB)) MB)"
Write-Host "sha256 $hash"
