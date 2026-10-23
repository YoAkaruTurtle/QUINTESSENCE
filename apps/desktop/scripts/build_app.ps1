param(
    [string]$RepoRoot = "",
    [string]$LabRoot = "",
    [string]$CargoTarget = "",
    [switch]$SkipBundle,
    [switch]$DebugBuild,
    [switch]$AllowFallbackIcons
)

$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path (Split-Path $AppRoot -Parent) -Parent
}
if ([string]::IsNullOrWhiteSpace($LabRoot)) {
    $LabRoot = Join-Path (Split-Path $RepoRoot -Parent) 'QUINTESSENCE-Lab'
}
if ([string]::IsNullOrWhiteSpace($CargoTarget)) {
    $CargoTarget = Join-Path $LabRoot 'build\c12b\cargo-target'
}
$Build = Join-Path $LabRoot 'build\c12b'
$Evidence = Join-Path $Build 'evidence'
$Artifacts = Join-Path $Build 'artifacts'
$SidecarDist = Join-Path $Build 'sidecar-dist\pcs-bridge'
$SidecarExe = Join-Path $SidecarDist 'pcs-bridge.exe'
New-Item -ItemType Directory -Force -Path $Build, $Evidence, $Artifacts | Out-Null

if (-not (Test-Path $SidecarExe)) {
    throw 'build the one-folder sidecar first: scripts\build_sidecar.ps1'
}
if (-not (Test-Path (Join-Path $AppRoot 'dist\index.html'))) {
    Write-Host '[app] frontend dist is built by the Tauri beforeBuildCommand'
}

# The owner-supplied artwork stays outside the repository.  A formal build
# scales it into the ignored icon directory and the public brand image; a clean
# checkout without the artwork may use the neutral build.rs fallback.
$brandSource = if ($env:QUINTESSENCE_BRAND_ICON) { $env:QUINTESSENCE_BRAND_ICON } else { Join-Path $Build 'brand\quintessence-icon.png' }
$brandPython = Join-Path $Build 'venv\Scripts\python.exe'
if (Test-Path $brandSource) {
    if (-not (Test-Path $brandPython)) { throw 'Pillow Python not found for brand assets' }
    & $brandPython (Join-Path $AppRoot 'scripts\make_brand_assets.py') `
        --source $brandSource --app-root $AppRoot `
        --evidence (Join-Path $Evidence 'brand-assets.json') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'brand asset generation failed' }
    Write-Host '[app] brand assets generated from the owner-supplied artwork'
}
elseif (-not $AllowFallbackIcons) {
    throw "brand artwork not found: $brandSource (pass -AllowFallbackIcons only for a non-brand build)"
}

# The resource mapping is injected here so the source configuration never
# contains a machine-specific build path.
$bundleConfig = Join-Path $Build 'tauri.bundle.json'
$bundleDocument = [ordered]@{
    bundle = [ordered]@{
        resources = [ordered]@{
            $SidecarDist = 'resources/sidecar/pcs-bridge'
        }
    }
}
[System.IO.File]::WriteAllText(
    $bundleConfig,
    ($bundleDocument | ConvertTo-Json -Depth 6),
    (New-Object System.Text.UTF8Encoding($false))
)

# Tauri's Windows NSIS bundler downloads its tools on first use.  Reuse the
# cached Lab copy when the selected cargo target has no tools yet.
$nsisTarget = Join-Path $CargoTarget '.tauri\NSIS'
if (-not (Test-Path $nsisTarget)) {
    $cached = Join-Path $LabRoot 'c12b-tech-spike\src-tauri\target\.tauri\NSIS'
    if (Test-Path $cached) {
        New-Item -ItemType Directory -Force -Path (Split-Path $nsisTarget -Parent) | Out-Null
        Copy-Item -Recurse -Force $cached $nsisTarget
        Write-Host '[app] reused cached NSIS tools'
    }
}

$cargoHome = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { Join-Path $env:USERPROFILE '.cargo' }
$env:CARGO_TARGET_DIR = $CargoTarget
# Keep Cargo registry, user profile, repository and Lab paths out of release
# binaries.  This is a release build setting, not a behaviour change.
$env:RUSTFLAGS = "--remap-path-prefix=$cargoHome=cargo --remap-path-prefix=$env:USERPROFILE=user --remap-path-prefix=$RepoRoot=repo --remap-path-prefix=$LabRoot=lab"

$profile = if ($DebugBuild) { 'debug' } else { 'release' }
$arguments = @('tauri', 'build', '--config', $bundleConfig)
if (-not $SkipBundle) {
    $arguments += @('--bundles', 'nsis')
}
if ($DebugBuild) {
    $arguments += '--debug'
}
Write-Host "[app] tauri build profile=$profile"
$buildLog = Join-Path $Build 'tauri-build.log'
Push-Location $AppRoot
try {
    $ErrorActionPreference = 'Continue'
    & pnpm @arguments *> $buildLog
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($exitCode -ne 0) {
        Get-Content $buildLog -Tail 40
        throw "tauri build failed with exit code $exitCode"
    }
}
finally {
    Pop-Location
}

$appExe = Join-Path $CargoTarget "$profile\quintessence-desktop.exe"
if (-not (Test-Path $appExe)) { throw "app executable not found: $appExe" }

$installer = $null
if (-not $SkipBundle) {
    $installer = Get-ChildItem (Join-Path $CargoTarget "$profile\bundle\nsis\*.exe") -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -eq $installer) { throw 'NSIS installer not found' }
    Copy-Item $installer.FullName (Join-Path $Artifacts $installer.Name) -Force
}

$sidecar = Get-Content (Join-Path $Evidence 'sidecar-build.json') -Raw | ConvertFrom-Json
$payload = [ordered]@{
    built_at = (Get-Date).ToString('o')
    profile = $profile
    rustc = (& rustc --version)
    cargo = (& cargo --version)
    node = (& node --version)
    pnpm = (& pnpm --version)
    rustflags = $env:RUSTFLAGS
    app_executable = $appExe
    app_executable_sha256 = (Get-FileHash $appExe -Algorithm SHA256).Hash.ToLowerInvariant()
    app_executable_bytes = (Get-Item $appExe).Length
    sidecar_sha256 = $sidecar.executable_sha256
    sidecar_bytes = $sidecar.executable_bytes
    sidecar_dist_files = $sidecar.dist_files
    bundle_resources = [ordered]@{
        source = $SidecarDist
        target = 'resources/sidecar/pcs-bridge'
    }
    external_bin = @()
    installer = if ($null -eq $installer) { $null } else { $installer.Name }
    installer_sha256 = if ($null -eq $installer) { $null } else { (Get-FileHash $installer.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
    installer_bytes = if ($null -eq $installer) { $null } else { $installer.Length }
    frontend_files = (Get-ChildItem (Join-Path $AppRoot 'dist') -Recurse -File).Count
    frontend_bytes = ((Get-ChildItem (Join-Path $AppRoot 'dist') -Recurse -File) | Measure-Object Length -Sum).Sum
}
$payloadJson = $payload | ConvertTo-Json -Depth 6
$payloadJson | Set-Content -Path (Join-Path $Evidence 'app-build.json') -Encoding utf8
$payloadJson
