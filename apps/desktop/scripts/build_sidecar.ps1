param(
    [string]$RepoRoot = "",
    [string]$LabRoot = "",
    [string]$Python = "",
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path (Split-Path $AppRoot -Parent) -Parent
}
if ([string]::IsNullOrWhiteSpace($LabRoot)) {
    $LabRoot = Join-Path (Split-Path $RepoRoot -Parent) 'QUINTESSENCE-Lab'
}
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = Join-Path $LabRoot 'build\c12b\venv\Scripts\python.exe'
}
$Build = Join-Path $LabRoot 'build\c12b'
$Stage = Join-Path $Build 'sidecar-stage'
$Work = Join-Path $Build 'pyinstaller-work'
$DistRoot = Join-Path $Build 'sidecar-dist'
$Dist = Join-Path $DistRoot 'pcs-bridge'
$Exe = Join-Path $Dist 'pcs-bridge.exe'
$Evidence = Join-Path $Build 'evidence'
$Spec = Join-Path $Build 'pcs_desktop.spec'
New-Item -ItemType Directory -Force -Path $Build, $Evidence | Out-Null

if (-not (Test-Path $Python)) {
    throw "PyInstaller Python not found: $Python (create the Lab venv and install pyinstaller==6.16.0)"
}
$PcsSource = Join-Path $RepoRoot 'src\quintessence_pcs'
if (-not (Test-Path $PcsSource)) { throw "PCS source not found under $RepoRoot" }

Write-Host '[sidecar] staging sources outside the repository'
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
New-Item -ItemType Directory -Force -Path $Stage | Out-Null
Copy-Item (Join-Path $AppRoot 'sidecar\*.py') $Stage
Copy-Item $PcsSource (Join-Path $Stage 'quintessence_pcs') -Recurse
Get-ChildItem $Stage -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
Get-ChildItem $Stage -Recurse -File -Include '*.pyc', '*.pyo' | Remove-Item -Force

$stageLiteral = $Stage.Replace('\', '\\')
$specText = @"
# Generated one-folder sidecar spec for the C12b desktop bridge.
from pathlib import Path

stage = Path(r'$stageLiteral')
migrations = stage / 'quintessence_pcs' / 'resources' / 'migrations'

a = Analysis(
    [str(stage / 'pcs_bridge.py')],
    pathex=[str(stage)],
    binaries=[],
    datas=[(str(migrations), 'resources/migrations')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc', 'doctest', 'test'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='pcs-bridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='pcs-bridge',
)
"@
Set-Content -Path $Spec -Value $specText -Encoding utf8

Write-Host '[sidecar] PyInstaller one-folder'
if (Test-Path $Dist) { Remove-Item -Recurse -Force $Dist }
if (Test-Path $Work) { Remove-Item -Recurse -Force $Work }
Push-Location $Build
try {
    $buildLog = Join-Path $Build 'sidecar-build.log'
    $ErrorActionPreference = 'Continue'
    & $Python -m PyInstaller --noconfirm --clean --distpath $DistRoot --workpath $Work $Spec *> $buildLog
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($exitCode -ne 0) {
        Get-Content $buildLog -Tail 30
        throw "PyInstaller failed with exit code $exitCode"
    }
}
finally {
    Pop-Location
}
if (-not (Test-Path $Exe)) { throw "packaged sidecar not found: $Exe" }
if (-not (Test-Path (Join-Path $Dist '_internal'))) { throw 'one-folder _internal directory missing' }

$files = @(Get-ChildItem $Dist -Recurse -File)
$dlls = @($files | Where-Object { $_.Extension -in '.dll', '.pyd' })
$internal = @(Get-ChildItem (Join-Path $Dist '_internal') -Recurse -File)
$migrations = @($files | Where-Object { $_.FullName -match 'resources\\migrations\\\d{4}_.*\.sql$' })
$manifest = [ordered]@{
    built_at = (Get-Date).ToString('o')
    executable_sha256 = (Get-FileHash $Exe -Algorithm SHA256).Hash.ToLowerInvariant()
    executable_bytes = (Get-Item $Exe).Length
    dist_bytes = ($files | Measure-Object Length -Sum).Sum
    dist_files = $files.Count
    internal_files = $internal.Count
    dll_and_pyd_files = $dlls.Count
    migration_sql_files = $migrations.Count
    files = @($files | ForEach-Object {
        [ordered]@{
            path = $_.FullName.Substring($Dist.Length + 1).Replace('\', '/')
            bytes = $_.Length
            sha256 = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    })
}
$manifestJson = $manifest | ConvertTo-Json -Depth 5
Set-Content -Path (Join-Path $Evidence 'sidecar-manifest.json') -Value $manifestJson -Encoding utf8
$summary = [ordered]@{
    built_at = $manifest.built_at
    executable_sha256 = $manifest.executable_sha256
    executable_bytes = $manifest.executable_bytes
    dist_bytes = $manifest.dist_bytes
    dist_files = $manifest.dist_files
    internal_files = $manifest.internal_files
    dll_and_pyd_files = $manifest.dll_and_pyd_files
    migration_sql_files = $manifest.migration_sql_files
    python = (& $Python --version)
    pyinstaller = (& $Python -m PyInstaller --version)
}
$summary | ConvertTo-Json | Set-Content -Path (Join-Path $Evidence 'sidecar-build.json') -Encoding utf8
$summary | ConvertTo-Json
Write-Host "[sidecar] built: $Exe"

if (-not $SkipTests) {
    Write-Host '[sidecar] packaged smoke test'
    $env:QUINTESSENCE_DESKTOP_SIDECAR_EXE = $Exe
    Push-Location $RepoRoot
    try {
        & python -m unittest tests.desktop.test_packaged -v
        if ($LASTEXITCODE -ne 0) { throw 'packaged sidecar test failed' }
    }
    finally {
        Remove-Item Env:\QUINTESSENCE_DESKTOP_SIDECAR_EXE -ErrorAction SilentlyContinue
        Pop-Location
    }
}

Write-Host '[sidecar] private-data scan'
& python (Join-Path $AppRoot 'scripts\scan_private.py') --root $Dist --output (Join-Path $Evidence 'scan-sidecar.json') --json
if ($LASTEXITCODE -ne 0) { throw 'sidecar bundle scan failed' }
