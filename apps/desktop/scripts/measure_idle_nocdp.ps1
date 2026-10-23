param(
    [string]$Installer = "",
    [string]$InstallDir = "",
    [string]$LabRoot = "",
    [int]$RunCount = 5
)

$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path -Parent $PSScriptRoot
$RepoRoot = Split-Path (Split-Path $AppRoot -Parent) -Parent
if ([string]::IsNullOrWhiteSpace($LabRoot)) {
    $LabRoot = Join-Path (Split-Path $RepoRoot -Parent) 'QUINTESSENCE-Lab'
}
$Build = Join-Path $LabRoot 'build\c12b'
$Evidence = Join-Path $Build 'evidence'
$MeasureRoot = Join-Path $Build 'measure'
$DataRoot = Join-Path $Build 'measure-data'
if ([string]::IsNullOrWhiteSpace($InstallDir)) {
    $InstallDir = Join-Path $LabRoot 'measure-install\c12b'
}
New-Item -ItemType Directory -Force -Path $Evidence, $MeasureRoot, $DataRoot | Out-Null

if ([string]::IsNullOrWhiteSpace($Installer)) {
    $Installer = (Get-ChildItem (Join-Path $Build 'artifacts\*setup.exe') |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1).FullName
}
if (-not (Test-Path (Join-Path $InstallDir 'quintessence-desktop.exe'))) {
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    $install = Start-Process -FilePath $Installer -ArgumentList @('/S', "/D=$InstallDir") -Wait -PassThru
    if ($install.ExitCode -ne 0) { throw "installer exit code $($install.ExitCode)" }
    Start-Sleep -Seconds 2
}
$AppExe = Join-Path $InstallDir 'quintessence-desktop.exe'

function Get-TreeIds([int]$RootId) {
    $all = Get-CimInstance Win32_Process
    $ids = New-Object System.Collections.Generic.List[int]
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue($RootId)
    $ids.Add($RootId)
    while ($queue.Count -gt 0) {
        $parent = [int]$queue.Dequeue()
        foreach ($child in $all | Where-Object { $_.ParentProcessId -eq $parent }) {
            $ids.Add([int]$child.ProcessId)
            $queue.Enqueue([int]$child.ProcessId)
        }
    }
    return $ids
}

function Get-Sample([int]$RootId) {
    $rows = @()
    foreach ($id in (Get-TreeIds $RootId)) {
        $process = Get-Process -Id $id -ErrorAction SilentlyContinue
        if ($null -eq $process) { continue }
        $rows += [pscustomobject]@{
            pid = $process.Id
            name = $process.ProcessName
            working_set_bytes = [int64]$process.WorkingSet64
            private_bytes = [int64]$process.PrivateMemorySize64
        }
    }
    $shell = @($rows | Where-Object { $_.name -ne 'pcs-bridge' -and $_.name -ne 'conhost' })
    $webview = @($rows | Where-Object { $_.name -eq 'msedgewebview2' })
    $sidecar = @($rows | Where-Object { $_.name -eq 'pcs-bridge' })
    return [pscustomobject]@{
        processes = $rows
        full_ws = [int64](($rows | Measure-Object working_set_bytes -Sum).Sum)
        full_private = [int64](($rows | Measure-Object private_bytes -Sum).Sum)
        shell_ws = [int64](($shell | Measure-Object working_set_bytes -Sum).Sum)
        shell_private = [int64](($shell | Measure-Object private_bytes -Sum).Sum)
        webview_ws = [int64](($webview | Measure-Object working_set_bytes -Sum).Sum)
        webview_private = [int64](($webview | Measure-Object private_bytes -Sum).Sum)
        sidecar_ws = [int64](($sidecar | Measure-Object working_set_bytes -Sum).Sum)
        sidecar_private = [int64](($sidecar | Measure-Object private_bytes -Sum).Sum)
    }
}

function Get-Median([double[]]$Values) {
    $sorted = $Values | Sort-Object
    $count = $sorted.Count
    if ($count % 2 -eq 1) { return [double]$sorted[[int](($count - 1) / 2)] }
    return ([double]$sorted[$count / 2 - 1] + [double]$sorted[$count / 2]) / 2
}

$config = Join-Path $MeasureRoot 'idle-nocdp-host.json'
$data = Join-Path $DataRoot 'idle-nocdp'
New-Item -ItemType Directory -Force -Path $data | Out-Null
& python (Join-Path $AppRoot 'scripts\make_lab_host.py') `
    --output $config --data-dir $data --database 'idle.sqlite' --mode answered | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'config generation failed' }

$samples = @()
for ($index = 1; $index -le $RunCount; $index++) {
    $previousConfig = $env:QUINTESSENCE_DESKTOP_CONFIG
    $previousArgs = $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS
    $env:QUINTESSENCE_DESKTOP_CONFIG = $config
    Remove-Item Env:\WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS -ErrorAction SilentlyContinue
    try {
        $app = Start-Process -FilePath $AppExe -PassThru
    }
    finally {
        if ($null -eq $previousConfig) { Remove-Item Env:\QUINTESSENCE_DESKTOP_CONFIG -ErrorAction SilentlyContinue }
        else { $env:QUINTESSENCE_DESKTOP_CONFIG = $previousConfig }
        if ($null -ne $previousArgs) { $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = $previousArgs }
    }
    Start-Sleep -Seconds 8
    $sample = Get-Sample $app.Id
    $samples += $sample
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    & taskkill.exe /PID $app.Id /T /F 2>&1 | Out-Null
    Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" |
        Where-Object { $_.CommandLine -like '*com.quintessence.desktop*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $ErrorActionPreference = $previous
    Start-Sleep -Milliseconds 900
    Write-Host "[idle-nocdp] run $index full_ws=$([math]::Round($sample.full_ws/1MB,2))MiB full_private=$([math]::Round($sample.full_private/1MB,2))MiB"
}

$summary = [ordered]@{
    full_ws_mib = [ordered]@{
        median = [math]::Round((Get-Median ($samples | ForEach-Object { $_.full_ws })) / 1MB, 2)
        min = [math]::Round((($samples | ForEach-Object { $_.full_ws }) | Measure-Object -Minimum).Minimum / 1MB, 2)
        max = [math]::Round((($samples | ForEach-Object { $_.full_ws }) | Measure-Object -Maximum).Maximum / 1MB, 2)
    }
    full_private_mib = [ordered]@{
        median = [math]::Round((Get-Median ($samples | ForEach-Object { $_.full_private })) / 1MB, 2)
        min = [math]::Round((($samples | ForEach-Object { $_.full_private }) | Measure-Object -Minimum).Minimum / 1MB, 2)
        max = [math]::Round((($samples | ForEach-Object { $_.full_private }) | Measure-Object -Maximum).Maximum / 1MB, 2)
    }
    shell_ws_mib = [math]::Round((Get-Median ($samples | ForEach-Object { $_.shell_ws })) / 1MB, 2)
    webview_ws_mib = [math]::Round((Get-Median ($samples | ForEach-Object { $_.webview_ws })) / 1MB, 2)
    webview_private_mib = [math]::Round((Get-Median ($samples | ForEach-Object { $_.webview_private })) / 1MB, 2)
    sidecar_ws_mib = [math]::Round((Get-Median ($samples | ForEach-Object { $_.sidecar_ws })) / 1MB, 2)
    sidecar_private_mib = [math]::Round((Get-Median ($samples | ForEach-Object { $_.sidecar_private })) / 1MB, 2)
}
$report = [ordered]@{
    measured_at = (Get-Date).ToString('o')
    tool = 'PowerShell Get-Process WorkingSet64/PrivateMemorySize64; no WebView2 remote-debugging arguments'
    note = 'Idle-only supplementary run; the interaction table in resource-measurements.json uses the CDP test harness.'
    runs = $samples
    summary = $summary
}
$report | ConvertTo-Json -Depth 8 | Set-Content -Path (Join-Path $Evidence 'resource-idle-nocdp.json') -Encoding utf8
$summary | ConvertTo-Json -Depth 4
