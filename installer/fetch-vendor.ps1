# Download the third-party installer that ships inside the WeatherPi Companion installer into
# installer\vendor\ (git-ignored), and check it against its pinned sha256.
# Tailscale isn't fetched here: the installer downloads it at install time (D25).
#
#   powershell -ExecutionPolicy Bypass -File installer\fetch-vendor.ps1

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Vendor = Join-Path $PSScriptRoot "vendor"

# Pinned: keep in step with rpi-usb-gadget in the image (installer\vendor\README.md).
$Files = @(
    @{
        Name   = "rpi-usb-gadget-driver-setup.exe"
        Url    = "https://github.com/raspberrypi/rpi-usb-gadget/releases/download/v1.0.6/rpi-usb-gadget-driver-setup.exe"
        Sha256 = "9edf7f819cb9afa9126cda22b08ee91dff119a4557f590bb4d993daa78132c1e"
    }
)

New-Item -ItemType Directory -Force $Vendor | Out-Null
foreach ($f in $Files) {
    $dest = Join-Path $Vendor $f.Name
    if ((Test-Path $dest) -and ((Get-FileHash $dest -Algorithm SHA256).Hash -eq $f.Sha256)) {
        Write-Host "ok        $($f.Name)"
        continue
    }
    Write-Host "download  $($f.Name)"
    $part = "$dest.part"
    $ProgressPreference = "SilentlyContinue"  # Invoke-WebRequest is very slow with the progress bar
    Invoke-WebRequest -Uri $f.Url -OutFile $part -UseBasicParsing
    $got = (Get-FileHash $part -Algorithm SHA256).Hash
    if ($got -ne $f.Sha256) {
        Remove-Item $part
        throw "$($f.Name): sha256 is $got, expected $($f.Sha256)"
    }
    Move-Item -Force $part $dest
    Write-Host "ok        $($f.Name)"
}
