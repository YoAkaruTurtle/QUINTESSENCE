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
    $Output = Join-Path $LabRoot 'build\c12b\evidence\l2-probe.json'
}
New-Item -ItemType Directory -Force -Path (Split-Path $Output -Parent) | Out-Null

$os = Get-CimInstance Win32_OperatingSystem
$computer = Get-CimInstance Win32_ComputerSystem
$sandboxCommand = Get-Command WindowsSandbox.exe -ErrorAction SilentlyContinue
$feature = $null
$featureError = $null
try {
    $feature = Get-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -ErrorAction Stop
}
catch {
    $featureError = $_.Exception.GetType().Name
}
$hypervisor = $false
try {
    $hypervisor = (Get-CimInstance Win32_ComputerSystem).HypervisorPresent
}
catch {
    $hypervisor = $false
}

$virtualMarkers = @('VirtualBox', 'VMware', 'Virtual Machine', 'Hyper-V', 'QEMU', 'KVM', 'Parallels')
$looksVirtual = $false
$identity = "$($computer.Manufacturer) $($computer.Model)"
foreach ($marker in $virtualMarkers) {
    if ($identity -match [regex]::Escape($marker)) { $looksVirtual = $true }
}

$payload = [ordered]@{
    checked_at = (Get-Date).ToString('o')
    os = [ordered]@{
        caption = $os.Caption
        version = $os.Version
        build = $os.BuildNumber
        architecture = $os.OSArchitecture
    }
    computer = [ordered]@{
        manufacturer = $computer.Manufacturer
        model = $computer.Model
        hypervisor_present = $hypervisor
    }
    windows_sandbox = [ordered]@{
        command_present = ($null -ne $sandboxCommand)
        feature_state = if ($null -ne $feature) { "$($feature.State)" } else { $null }
        feature_error = $featureError
    }
    clean_windows_guest = [ordered]@{
        available = $false
        looks_virtual_machine = $looksVirtual
        reason = 'No disposable Windows guest, VM or independent Windows machine is available in this environment.'
    }
    l2 = 'UNVERIFIED'
    webview2_missing = 'UNVERIFIED'
    offline_install = 'UNVERIFIED'
    no_global_python = 'L1 only; L2 not executed on a clean machine'
    passed_levels = @('L0', 'L1')
    unverified_levels = @('L2', 'WebView2 missing', 'offline install')
}
$payload | ConvertTo-Json -Depth 6 | Set-Content -Path $Output -Encoding utf8
$payload | ConvertTo-Json -Depth 6
