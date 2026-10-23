param(
    [string]$LabRoot = "",
    [string]$Output = ""
)

$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path -Parent $PSScriptRoot
$RepoRoot = Split-Path (Split-Path $AppRoot -Parent) -Parent
if ([string]::IsNullOrWhiteSpace($LabRoot)) {
    $LabRoot = Join-Path (Split-Path $RepoRoot -Parent) 'QUINTESSENCE-Lab'
}
if ([string]::IsNullOrWhiteSpace($Output)) {
    $Output = Join-Path $LabRoot 'build\c12b\evidence\webview2.json'
}
New-Item -ItemType Directory -Force -Path (Split-Path $Output -Parent) | Out-Null

$clientId = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
$registryPaths = @(
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$clientId",
    "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$clientId",
    "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$clientId"
)
$registryVersions = @()
foreach ($path in $registryPaths) {
    try {
        $value = (Get-ItemProperty -Path $path -ErrorAction Stop).pv
        if ($value) { $registryVersions += $value }
    }
    catch {
        # Key absent is expected on some installations.
    }
}

$runtimeRoots = @(
    (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\EdgeWebView\Application'),
    (Join-Path $env:ProgramFiles 'Microsoft\EdgeWebView\Application')
) | Where-Object { $_ -and (Test-Path $_) }

$runtimeVersions = @()
$runtimePaths = @()
foreach ($root in $runtimeRoots) {
    foreach ($directory in Get-ChildItem $root -Directory -ErrorAction SilentlyContinue) {
        $exe = Join-Path $directory.FullName 'msedgewebview2.exe'
        if (Test-Path $exe) {
            $fileVersion = (Get-Item $exe).VersionInfo.FileVersion
            $runtimeVersions += $fileVersion
            $runtimePaths += $exe
        }
    }
}

$detected = ($registryVersions.Count -gt 0) -or ($runtimeVersions.Count -gt 0)
$installerMode = 'unknown'
$configPath = Join-Path $AppRoot 'src-tauri\tauri.conf.json'
if (Test-Path $configPath) {
    $config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($config.bundle.windows.webviewInstallMode.type) {
        $installerMode = $config.bundle.windows.webviewInstallMode.type
    }
}

$payload = [ordered]@{
    checked_at = (Get-Date).ToString('o')
    detection = 'HKLM/HKCU EdgeUpdate client key + msedgewebview2.exe file version'
    registry_versions = @($registryVersions | Select-Object -Unique)
    runtime_versions = @($runtimeVersions | Select-Object -Unique)
    runtime_paths = @($runtimePaths)
    detected = $detected
    installer_mode = $installerMode
    offline_installer_built = $false
    missing_scenario = if ($detected) { 'UNVERIFIED' } else { 'UNKNOWN' }
    missing_reason = if ($detected) {
        'The runtime is installed on this machine; no clean Windows guest is available to remove it.'
    } else {
        'No runtime was detected; a missing-runtime launch was not attempted.'
    }
    passed = $detected
}
$payload | ConvertTo-Json -Depth 5 | Set-Content -Path $Output -Encoding utf8
$payload | ConvertTo-Json -Depth 5
