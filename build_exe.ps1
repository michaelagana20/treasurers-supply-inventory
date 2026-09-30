$ErrorActionPreference = "Stop"
$Python = "C:\Program Files\Blender Foundation\Blender 5.0\5.0\python\bin\python.exe"
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
$Dependencies = Join-Path $Project ".build-deps"

if (-not (Test-Path $Python)) {
    throw "Build Python was not found at $Python"
}
if (-not (Test-Path (Join-Path $Dependencies "PyInstaller"))) {
    throw "Build dependencies are missing. Install PyInstaller, pystray, and Pillow into .build-deps first."
}

$env:PYTHONPATH = $Dependencies
& $Python (Join-Path $Project "build_icon.py")
if ($LASTEXITCODE -ne 0) {
    throw "The application icon build failed."
}
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name TreasurersSupplyInventory `
    --icon "app_icon.ico" `
    --add-data "web;web" `
    --paths $Dependencies `
    --exclude-module numpy `
    --hidden-import pystray._win32 `
    server.py

if ($LASTEXITCODE -ne 0) {
    throw "The executable build failed."
}

$BuiltExe = Join-Path $Project "dist\TreasurersSupplyInventory.exe"
$RunnableExe = Join-Path $Project "TreasurersSupplyInventory.exe"
Copy-Item -LiteralPath $BuiltExe -Destination $RunnableExe -Force
Write-Host "Built: $RunnableExe"
