param(
    [string]$Installer = "",
    [string]$InstallDir = "",
    [string]$LabRoot = "",
    [int]$Port = 9227,
    [int]$Runs = 5
)

$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path -Parent $PSScriptRoot
$RepoRoot = Split-Path (Split-Path $AppRoot -Parent) -Parent
if ([string]::IsNullOrWhiteSpace($LabRoot)) {
    $LabRoot = Join-Path (Split-Path $RepoRoot -Parent) 'QUINTESSENCE-Lab'
}
$Build = Join-Path $LabRoot 'build\c12b'
$Evidence = Join-Path $Build 'evidence'
$Artifacts = Join-Path $Build 'artifacts'
$MeasureRoot = Join-Path $Build 'measure'
$DataRoot = Join-Path $Build 'measure-data'
if ([string]::IsNullOrWhiteSpace($InstallDir)) {
    $InstallDir = Join-Path $LabRoot 'measure-install\c12b'
}
New-Item -ItemType Directory -Force -Path $Evidence, $MeasureRoot, $DataRoot | Out-Null

if ([string]::IsNullOrWhiteSpace($Installer)) {
    $candidate = Get-ChildItem (Join-Path $Artifacts '*setup.exe') -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -eq $candidate) { throw 'no installer in the Lab artifact directory' }
    $Installer = $candidate.FullName
}

if (-not (Test-Path (Join-Path $InstallDir 'quintessence-desktop.exe'))) {
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    $install = Start-Process -FilePath $Installer -ArgumentList @('/S', "/D=$InstallDir") -Wait -PassThru
    if ($install.ExitCode -ne 0) { throw "installer exit code $($install.ExitCode)" }
    Start-Sleep -Seconds 2
}
$AppExe = Join-Path $InstallDir 'quintessence-desktop.exe'
if (-not (Test-Path $AppExe)) { throw 'installed app executable not found' }

function Get-ProcessTreeIds([int]$RootId) {
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

function Get-Sample([int]$RootId, [string]$Phase) {
    $ids = New-Object System.Collections.Generic.List[int]
    foreach ($id in (Get-ProcessTreeIds $RootId)) { $ids.Add([int]$id) }
    # A WebView2 browser process can be reused across a rapid restart; include
    # the processes that carry this application's user-data directory.
    $extra = Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" |
        Where-Object { $_.CommandLine -like '*com.quintessence.desktop*' }
    foreach ($process in $extra) {
        if (-not $ids.Contains([int]$process.ProcessId)) { $ids.Add([int]$process.ProcessId) }
    }
    $rows = @()
    foreach ($id in $ids) {
        $process = Get-Process -Id $id -ErrorAction SilentlyContinue
        if ($null -eq $process) { continue }
        $rows += [pscustomobject]@{
            pid = $process.Id
            name = $process.ProcessName
            working_set_bytes = [int64]$process.WorkingSet64
            private_bytes = [int64]$process.PrivateMemorySize64
        }
    }
    $webview = @($rows | Where-Object { $_.name -eq 'msedgewebview2' })
    $sidecar = @($rows | Where-Object { $_.name -eq 'pcs-bridge' })
    $shell = @($rows | Where-Object { $_.name -ne 'pcs-bridge' -and $_.name -ne 'conhost' })
    $full = $rows
    return [pscustomobject]@{
        phase = $Phase
        processes = $rows
        shell = @{
            working_set_bytes = [int64](($shell | Measure-Object working_set_bytes -Sum).Sum)
            private_bytes = [int64](($shell | Measure-Object private_bytes -Sum).Sum)
        }
        webview = @{
            processes = $webview.Count
            working_set_bytes = [int64](($webview | Measure-Object working_set_bytes -Sum).Sum)
            private_bytes = [int64](($webview | Measure-Object private_bytes -Sum).Sum)
        }
        sidecar = @{
            processes = $sidecar.Count
            working_set_bytes = [int64](($sidecar | Measure-Object working_set_bytes -Sum).Sum)
            private_bytes = [int64](($sidecar | Measure-Object private_bytes -Sum).Sum)
        }
        full = @{
            working_set_bytes = [int64](($full | Measure-Object working_set_bytes -Sum).Sum)
            private_bytes = [int64](($full | Measure-Object private_bytes -Sum).Sum)
        }
    }
}

function Wait-Cdp([int]$TimeoutSeconds = 60) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/json/list" -TimeoutSec 2
            if ($response.StatusCode -eq 200 -and $response.Content -match 'webSocketDebuggerUrl') { return }
        }
        catch {
            Start-Sleep -Milliseconds 100
        }
    }
    throw 'WebView2 debug endpoint did not become ready'
}

function Stop-App([int]$ProcessId) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    & taskkill.exe /PID $ProcessId /T /F 2>&1 | Out-Null
    Get-Process -Name 'pcs-bridge' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" |
        Where-Object { $_.CommandLine -like '*com.quintessence.desktop*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $deadline = (Get-Date).AddSeconds(6)
    while ((Get-Date) -lt $deadline) {
        if (@(Get-Process -Name 'quintessence-desktop', 'pcs-bridge' -ErrorAction SilentlyContinue).Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    }
    $ErrorActionPreference = $previous
    Start-Sleep -Milliseconds 500
}

function Start-App([string]$ConfigPath, [switch]$HighDpi) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" |
        Where-Object { $_.CommandLine -like '*com.quintessence.desktop*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $ErrorActionPreference = $previous
    Start-Sleep -Milliseconds 250
    $previousPath = $env:PATH
    $previousConfig = $env:QUINTESSENCE_DESKTOP_CONFIG
    $previousArgs = $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $env:QUINTESSENCE_DESKTOP_CONFIG = $ConfigPath
    $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = "--remote-debugging-port=$Port"
    try {
        $process = Start-Process -FilePath $AppExe -PassThru
    }
    finally {
        $env:PATH = $previousPath
        if ($null -eq $previousConfig) { Remove-Item Env:\QUINTESSENCE_DESKTOP_CONFIG -ErrorAction SilentlyContinue }
        else { $env:QUINTESSENCE_DESKTOP_CONFIG = $previousConfig }
        if ($null -eq $previousArgs) { Remove-Item Env:\WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS -ErrorAction SilentlyContinue }
        else { $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = $previousArgs }
    }
    return $process
}

function Get-Median([double[]]$Values) {
    $sorted = $Values | Sort-Object
    $count = $sorted.Count
    if ($count -eq 0) { return 0 }
    if ($count % 2 -eq 1) { return [double]$sorted[[int](($count - 1) / 2)] }
    return ([double]$sorted[$count / 2 - 1] + [double]$sorted[$count / 2]) / 2
}

function To-MiB([double]$Bytes) { return [math]::Round($Bytes / 1MB, 2) }

$measurements = @()
for ($index = 1; $index -le $Runs; $index++) {
    $tag = "run-$index"
    $data = Join-Path $DataRoot $tag
    New-Item -ItemType Directory -Force -Path $data | Out-Null
    $config = Join-Path $MeasureRoot "$tag-host.json"
    & python (Join-Path $AppRoot 'scripts\make_lab_host.py') `
        --output $config --data-dir $data --database 'measure.sqlite' --mode answered --delay-ms 1500 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "config generation failed for $tag" }

    $started = Get-Date
    $app = Start-App $config
    $coldMs = $null
    $idleSamples = @()
    $interactionSamples = @()
    $triggerPhase = $null
    try {
        Wait-Cdp
        $probe = & node (Join-Path $AppRoot 'scripts\ui_probe.mjs') $Port
        if ($LASTEXITCODE -ne 0) { throw "ui probe failed for $tag" }
        $coldMs = [math]::Round(((Get-Date) - $started).TotalMilliseconds, 1)
        $probeState = $probe | ConvertFrom-Json
        Start-Sleep -Seconds 5
        for ($sampleIndex = 0; $sampleIndex -lt 5; $sampleIndex++) {
            $idleSamples += Get-Sample $app.Id 'idle'
            Start-Sleep -Milliseconds 400
        }

        $triggerLog = Join-Path $MeasureRoot "$tag-trigger.log"
        $trigger = Start-Process -FilePath 'node' `
            -ArgumentList @((Join-Path $AppRoot 'scripts\ui_trigger.mjs'), $Port) `
            -PassThru -NoNewWindow -RedirectStandardOutput $triggerLog
        $deadline = (Get-Date).AddSeconds(75)
        while (-not $trigger.HasExited -and (Get-Date) -lt $deadline) {
            $interactionSamples += Get-Sample $app.Id 'interaction'
            Start-Sleep -Milliseconds 250
        }
        $trigger.WaitForExit(10000) | Out-Null
        if (-not $trigger.HasExited) { Stop-Process -Id $trigger.Id -Force }
        if (Test-Path $triggerLog) {
            $raw = Get-Content $triggerLog -Raw -Encoding UTF8
            if ($null -ne $raw) {
                $line = ([string]$raw).Trim()
                if ($line) {
                    try { $triggerPhase = ($line | ConvertFrom-Json).phase } catch { $triggerPhase = $null }
                }
                if ($null -eq $triggerPhase) {
                    Write-Host "[measure] trigger output: $line"
                }
            }
        }
    }
    finally {
        Stop-App $app.Id
    }

    $idleFullWs = ($idleSamples | ForEach-Object { $_.full.working_set_bytes })
    $idleFullPrivate = ($idleSamples | ForEach-Object { $_.full.private_bytes })
    $idleShellWs = ($idleSamples | ForEach-Object { $_.shell.working_set_bytes })
    $idleShellPrivate = ($idleSamples | ForEach-Object { $_.shell.private_bytes })
    $idleSidecarWs = ($idleSamples | ForEach-Object { $_.sidecar.working_set_bytes })
    $idleSidecarPrivate = ($idleSamples | ForEach-Object { $_.sidecar.private_bytes })
    $idleWebviewWs = ($idleSamples | ForEach-Object { $_.webview.working_set_bytes })
    $idleWebviewPrivate = ($idleSamples | ForEach-Object { $_.webview.private_bytes })
    $peakFullWs = if ($interactionSamples.Count) { ($interactionSamples | ForEach-Object { $_.full.working_set_bytes } | Measure-Object -Maximum).Maximum } else { 0 }
    $peakFullPrivate = if ($interactionSamples.Count) { ($interactionSamples | ForEach-Object { $_.full.private_bytes } | Measure-Object -Maximum).Maximum } else { 0 }
    $peakShellWs = if ($interactionSamples.Count) { ($interactionSamples | ForEach-Object { $_.shell.working_set_bytes } | Measure-Object -Maximum).Maximum } else { 0 }
    $peakSidecarWs = if ($interactionSamples.Count) { ($interactionSamples | ForEach-Object { $_.sidecar.working_set_bytes } | Measure-Object -Maximum).Maximum } else { 0 }

    $measurements += [ordered]@{
        run = $index
        cold_start_ms = $coldMs
        host_ready = $probeState.ready
        dpr = $probeState.dpr
        viewport = @{ width = $probeState.width; height = $probeState.height }
        idle = [ordered]@{
            full_ws_mib = To-MiB (Get-Median $idleFullWs)
            full_private_mib = To-MiB (Get-Median $idleFullPrivate)
            shell_ws_mib = To-MiB (Get-Median $idleShellWs)
            shell_private_mib = To-MiB (Get-Median $idleShellPrivate)
            sidecar_ws_mib = To-MiB (Get-Median $idleSidecarWs)
            sidecar_private_mib = To-MiB (Get-Median $idleSidecarPrivate)
            webview_ws_mib = To-MiB (Get-Median $idleWebviewWs)
            webview_private_mib = To-MiB (Get-Median $idleWebviewPrivate)
            processes = $idleSamples[0].processes
        }
        interaction = [ordered]@{
            phase = $triggerPhase
            samples = $interactionSamples.Count
            peak_full_ws_mib = To-MiB $peakFullWs
            peak_full_private_mib = To-MiB $peakFullPrivate
            peak_shell_ws_mib = To-MiB $peakShellWs
            peak_sidecar_ws_mib = To-MiB $peakSidecarWs
        }
        idle_samples = $idleSamples
        interaction_samples = $interactionSamples
    }
    Write-Host "[measure] $tag cold=${coldMs}ms idle_full_ws=$(To-MiB (Get-Median $idleFullWs))MiB peak_full_ws=$(To-MiB $peakFullWs)MiB phase=$triggerPhase"
}

function Get-Stat([string]$Path) {
    $values = @($validMeasurements | ForEach-Object { $item = $_; $cursor = $item; foreach ($part in $Path.Split('.')) { $cursor = $cursor.($part) }; [double]$cursor })
    return [ordered]@{
        median = [math]::Round((Get-Median $values), 2)
        min = [math]::Round(($values | Measure-Object -Minimum).Minimum, 2)
        max = [math]::Round(($values | Measure-Object -Maximum).Maximum, 2)
    }
}

$validMeasurements = @($measurements | Where-Object {
    $_.interaction.phase -eq 'answered' -and
    $_.idle.webview_ws_mib -gt 0 -and
    $_.idle.sidecar_ws_mib -gt 0
})
if ($validMeasurements.Count -eq 0) { throw 'no valid measurement run completed' }

$summary = [ordered]@{
    cold_start_ms = Get-Stat 'cold_start_ms'
    idle_full_ws_mib = Get-Stat 'idle.full_ws_mib'
    idle_full_private_mib = Get-Stat 'idle.full_private_mib'
    idle_shell_ws_mib = Get-Stat 'idle.shell_ws_mib'
    idle_shell_private_mib = Get-Stat 'idle.shell_private_mib'
    idle_sidecar_ws_mib = Get-Stat 'idle.sidecar_ws_mib'
    idle_sidecar_private_mib = Get-Stat 'idle.sidecar_private_mib'
    idle_webview_ws_mib = Get-Stat 'idle.webview_ws_mib'
    idle_webview_private_mib = Get-Stat 'idle.webview_private_mib'
    interaction_peak_full_ws_mib = Get-Stat 'interaction.peak_full_ws_mib'
    interaction_peak_full_private_mib = Get-Stat 'interaction.peak_full_private_mib'
    interaction_peak_shell_ws_mib = Get-Stat 'interaction.peak_shell_ws_mib'
    interaction_peak_sidecar_ws_mib = Get-Stat 'interaction.peak_sidecar_ws_mib'
}
$targets = [ordered]@{
    cold_start_ms = 2500
    idle_full_ws_mib = 350
    idle_full_private_mib = 200
    interaction_peak_full_ws_mib = 450
}
$baseline = [ordered]@{
    source = 'docs/design/C12b-TECH-SPIKE.md §8.3'
    idle_full_ws_mib = 424.57
    idle_full_private_mib = 207.95
    idle_shell_ws_mib = 385.68
    idle_shell_private_mib = 185.34
    interaction_peak_full_ws_mib = 428.29
    interaction_peak_full_private_mib = 266.70
    interaction_peak_shell_ws_mib = 387.66
}
$targetsMet = [ordered]@{
    cold_start = $summary.cold_start_ms.median -le $targets.cold_start_ms
    idle_full_ws = $summary.idle_full_ws_mib.median -le $targets.idle_full_ws_mib
    idle_full_private = $summary.idle_full_private_mib.median -le $targets.idle_full_private_mib
    interaction_peak_full_ws = $summary.interaction_peak_full_ws_mib.median -le $targets.interaction_peak_full_ws_mib
}
$report = [ordered]@{
    measured_at = (Get-Date).ToString('o')
    tool = 'PowerShell Get-Process WorkingSet64/PrivateMemorySize64; process tree via Win32_Process; median of 5 idle samples per run'
    runs_requested = $Runs
    runs_completed = $measurements.Count
    valid_runs = $validMeasurements.Count
    invalid_run_numbers = @($measurements | Where-Object { $validMeasurements -notcontains $_ } | ForEach-Object { $_.run })
    install_dir = $InstallDir
    summary = $summary
    targets = $targets
    targets_met = $targetsMet
    spike_baseline = $baseline
    runs = $measurements
    valid_run_numbers = @($validMeasurements | ForEach-Object { $_.run })
}
$report | ConvertTo-Json -Depth 12 | Set-Content -Path (Join-Path $Evidence 'resource-measurements.json') -Encoding utf8
Write-Host ($report | ConvertTo-Json -Depth 6)
if (-not ($targetsMet.cold_start -and $targetsMet.idle_full_ws -and $targetsMet.idle_full_private -and $targetsMet.interaction_peak_full_ws)) {
    Write-Host '[measure] one or more tentative targets were not met; the report records the measured values without adjustment.'
}
