# SPDX-License-Identifier: Apache-2.0
# MousePods on Windows: virtual environment, dependencies and a Start menu shortcut.
# Run from the repository:   powershell -ExecutionPolicy Bypass -File packaging\windows\install.ps1
# The MagicAAP driver is separate (it opens the AirPods' Bluetooth channel): see docs\WINDOWS.md.
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv .venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv .venv
    } else {
        throw "Python 3.10+ not found: install it from https://www.python.org/downloads/windows/"
    }
    if ($LASTEXITCODE -ne 0) { throw "could not create the virtual environment" }
}
& .venv\Scripts\python.exe -m pip install --upgrade pip
& .venv\Scripts\python.exe -m pip install -r requirements.txt

$pyw = Join-Path $Root ".venv\Scripts\pythonw.exe"
$lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "MousePods.lnk"
$sh = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$sh.TargetPath = $pyw
$sh.Arguments = '"' + (Join-Path $Root "MousePods.pyw") + '"'
$sh.WorkingDirectory = "$Root"
$sh.Description = "AirPods head tracking as a mouse"
$sh.Save()
Write-Host "Start menu shortcut: $lnk"

if (-not (Test-Path "native\out\NPClient64.dll")) {
    Write-Host "Note: native\out has no TrackIR DLLs. Build them on Linux with native/build.sh or take them"
    Write-Host "from the CI artifact 'native-dlls'; they are only needed for the TrackIR / FreeTrack output."
}
Write-Host "Done. Pair and connect the AirPods in Windows Settings, install MagicAAP, then open MousePods."
