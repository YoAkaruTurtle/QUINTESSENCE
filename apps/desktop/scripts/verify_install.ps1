param(
    [string]$Installer = "",
    [string]$InstallDir = "",
    [string]$LabRoot = "",
    [string]$Port = "9224",
    [string]$AiName = $env:QUINTESSENCE_AI_NAME
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
$ScenarioRoot = Join-Path $Build 'install-scenarios'
$Shots = Join-Path $Evidence 'installed'
$DataDir = Join-Path $Build 'install-data'
if ([string]::IsNullOrWhiteSpace($InstallDir)) {
    $InstallDir = Join-Path $LabRoot 'install\c12b'
}
New-Item -ItemType Directory -Force -Path $Evidence, $Shots, $ScenarioRoot, $DataDir | Out-Null

if ([string]::IsNullOrWhiteSpace($Installer)) {
    $candidate = Get-ChildItem (Join-Path $Artifacts '*setup.exe') -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -eq $candidate) { throw 'no installer in the Lab artifact directory' }
    $Installer = $candidate.FullName
}
$Installer = (Resolve-Path $Installer).Path

function Stop-App {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    & taskkill.exe /IM quintessence-desktop.exe /F 2>&1 | Out-Null
    & taskkill.exe /IM pcs-bridge.exe /F 2>&1 | Out-Null
    # WebView2 children can outlive the app briefly and would make the next
    # launch reuse an environment without the requested debugging port.
    Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" |
        Where-Object { $_.CommandLine -like '*com.quintessence.desktop*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $deadline = (Get-Date).AddSeconds(8)
    while ((Get-Date) -lt $deadline) {
        $remaining = @(Get-Process -Name 'quintessence-desktop', 'pcs-bridge' -ErrorAction SilentlyContinue)
        if ($remaining.Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    }
    $ErrorActionPreference = $previous
    Start-Sleep -Milliseconds 500
}

function Get-InstalledApp {
    $candidate = Get-ChildItem $InstallDir -Filter 'quintessence-desktop.exe' -File -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $candidate) {
        $candidate = Get-ChildItem $InstallDir -Filter '*.exe' -File |
            Where-Object { $_.Name -notmatch 'uninstall|pcs-bridge' } |
            Select-Object -First 1
    }
    return $candidate
}

function Start-AppWithConfig([string]$ConfigPath, [string]$Tag, [switch]$HighDpi) {
    $appExe = Get-InstalledApp
    if ($null -eq $appExe) { throw 'installed app executable not found' }
    $previousPath = $env:PATH
    $previousConfig = $env:QUINTESSENCE_DESKTOP_CONFIG
    $previousArgs = $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS
    $previousHome = $env:PYTHONHOME
    $previousPythonPath = $env:PYTHONPATH
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $env:PYTHONHOME = 'quintessence-no-python-home'
    $env:PYTHONPATH = 'quintessence-no-python-path'
    $env:QUINTESSENCE_DESKTOP_CONFIG = $ConfigPath
    $browserArguments = "--remote-debugging-port=$Port"
    if ($HighDpi) {
        $browserArguments = "$browserArguments --force-device-scale-factor=2"
    }
    $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = $browserArguments
    try {
        $process = Start-Process -FilePath $appExe.FullName -PassThru
    }
    finally {
        $env:PATH = $previousPath
        if ($null -eq $previousConfig) { Remove-Item Env:\QUINTESSENCE_DESKTOP_CONFIG -ErrorAction SilentlyContinue }
        else { $env:QUINTESSENCE_DESKTOP_CONFIG = $previousConfig }
        if ($null -eq $previousArgs) { Remove-Item Env:\WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS -ErrorAction SilentlyContinue }
        else { $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = $previousArgs }
        if ($null -eq $previousHome) { Remove-Item Env:\PYTHONHOME -ErrorAction SilentlyContinue }
        else { $env:PYTHONHOME = $previousHome }
        if ($null -eq $previousPythonPath) { Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue }
        else { $env:PYTHONPATH = $previousPythonPath }
    }
    Write-Host "[install] launched $Tag pid=$($process.Id)"
    return $process
}

function Wait-Cdp([int]$TimeoutSeconds = 60) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/json/list" -TimeoutSec 2
            if ($response.StatusCode -eq 200 -and $response.Content -match 'webSocketDebuggerUrl') {
                return
            }
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    throw 'WebView2 debug endpoint did not become ready'
}

function Invoke-Capture([string]$Scenario, [string]$Sizes, [string]$Tag, [string]$Scale = "", [string]$AiName = "") {
    $script = Join-Path $AppRoot 'scripts\ui_capture.mjs'
    $out = Join-Path $Shots $Tag
    New-Item -ItemType Directory -Force -Path $out | Out-Null
    $arguments = @($script, '--port', $Port, '--out-dir', $out, '--scenario', $Scenario, '--sizes', $Sizes)
    if ($Scale -ne "") { $arguments += @('--scale', $Scale) }
    if ($AiName -ne "") { $arguments += @('--ai-name', $AiName) }
    & node @arguments | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "ui capture failed for $Scenario" }
}

function Write-ScenarioConfig([string]$Tag, [string]$Mode, [switch]$Missing) {
    $directory = Join-Path $ScenarioRoot $Tag
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $config = Join-Path $directory 'host.json'
    if ($Missing) {
        return $config
    }
    $data = Join-Path $DataDir $Tag
    New-Item -ItemType Directory -Force -Path $data | Out-Null
    & python (Join-Path $AppRoot 'scripts\make_lab_host.py') `
        --output $config --data-dir $data --database 'desktop-lab.sqlite' --mode $Mode | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "lab config generation failed for $Tag" }
    if (-not (Test-Path $config)) { throw "lab config was not written: $config" }
    Write-Host "[install] config $Tag -> $config"
    return $config
}

function Get-Database([string]$Tag) {
    return Join-Path (Join-Path $DataDir $Tag) 'desktop-lab.sqlite'
}

function Invoke-StoreCheck([string]$Tag, [string]$Expect) {
    $database = Get-Database $Tag
    & python (Join-Path $AppRoot 'scripts\inspect_store.py') `
        --database $database --output (Join-Path $Evidence "store-$Tag.json") --expect-state $Expect | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "store check failed for $Tag" }
    Write-Host "[install] store check $Tag = $Expect"
}

function Count-Process([string]$Name) {
    return @(Get-Process -Name $Name -ErrorAction SilentlyContinue).Count
}

function Install-Package {
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    $install = Start-Process -FilePath $Installer -ArgumentList @('/S', "/D=$InstallDir") -Wait -PassThru
    if ($install.ExitCode -ne 0) { throw "installer exit code $($install.ExitCode)" }
    Start-Sleep -Seconds 2
    if (-not (Test-Path $InstallDir)) { throw 'install directory was not created' }
}

function Assert-InstallTree {
    $files = @(Get-ChildItem $InstallDir -Recurse -File)
    $bytes = ($files | Measure-Object Length -Sum).Sum
    $bad = @($files | Where-Object {
        $_.Extension -in '.sqlite', '.py', '.pdb', '.log' -or $_.Name -match '^node_modules$'
    })
    $probe = @($files | Where-Object { $_.Name -match 'external-probe' })
    if ($bad.Count -ne 0) { throw "install tree contains disallowed files: $($bad.Name -join ', ')" }
    if ($probe.Count -ne 0) { throw 'install tree contains an external probe' }

    $installedSidecar = Join-Path $InstallDir 'resources\sidecar\pcs-bridge'
    $manifest = Get-Content (Join-Path $Evidence 'sidecar-manifest.json') -Raw | ConvertFrom-Json
    $missing = @()
    $changed = @()
    foreach ($entry in $manifest.files) {
        $target = Join-Path $installedSidecar ($entry.path.Replace('/', '\'))
        if (-not (Test-Path $target)) { $missing += $entry.path; continue }
        if ((Get-Item $target).Length -ne $entry.bytes) { $changed += $entry.path; continue }
        $hash = (Get-FileHash $target -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash -ne $entry.sha256) { $changed += $entry.path }
    }
    $installedFiles = @(Get-ChildItem $installedSidecar -Recurse -File)
    $extra = @()
    foreach ($file in $installedFiles) {
        $relative = $file.FullName.Substring($installedSidecar.Length + 1).Replace('\', '/')
        if (-not ($manifest.files | Where-Object { $_.path -eq $relative })) { $extra += $relative }
    }
    $report = [ordered]@{
        files = $files.Count
        bytes = $bytes
        sidecar_files = $installedFiles.Count
        manifest_files = $manifest.files.Count
        missing = $missing
        changed = $changed
        extra = $extra
        external_probe = $probe.Count
        passed = ($missing.Count -eq 0 -and $changed.Count -eq 0 -and $extra.Count -eq 0)
    }
    $report | ConvertTo-Json -Depth 5 | Set-Content -Path (Join-Path $Evidence 'install-tree.json') -Encoding utf8
    Write-Host ($report | ConvertTo-Json -Depth 4)
    if (-not $report.passed) { throw 'installed sidecar does not match the build manifest' }
    return $report
}

Stop-App
Install-Package
# Verification must start from a clean synthetic data instance so the store
# evidence describes exactly one run, not an accumulation of earlier runs.
if (Test-Path $DataDir) { Remove-Item -Recurse -Force $DataDir }
if (Test-Path $ScenarioRoot) { Remove-Item -Recurse -Force $ScenarioRoot }
New-Item -ItemType Directory -Force -Path $DataDir, $ScenarioRoot | Out-Null
$tree = Assert-InstallTree

$scenarioResults = [ordered]@{}
$singleInstance = $null
$dataLock = $null

# --- answered: three window sizes, store evidence, single instance, data lock.
$config = Write-ScenarioConfig 'answered' 'answered'
$app = Start-AppWithConfig $config 'answered'
try {
    Wait-Cdp
    if ($AiName -ne "") {
        Invoke-Capture 'settings' '1280x800' 'settings' '' $AiName
        $scenarioResults['settings'] = [ordered]@{
            screenshots = 'evidence/installed/settings'
            ai_display_name = $AiName
            persisted = 'application config directory (outside the repository)'
        }
    }
    Invoke-Capture 'answered' '880x560,1280x800,1600x1000' 'answered'
    $store = Get-Database 'answered'
    Invoke-StoreCheck 'answered' 'answered'

    $sidecarProcess = Get-CimInstance Win32_Process -Filter "Name='pcs-bridge.exe'" | Select-Object -First 1
    $sidecarPathOk = $false
    if ($null -ne $sidecarProcess -and $sidecarProcess.ExecutablePath) {
        $sidecarPathOk = $sidecarProcess.ExecutablePath.StartsWith(
            (Join-Path $InstallDir 'resources\sidecar\pcs-bridge'),
            [System.StringComparison]::OrdinalIgnoreCase
        )
    }
    if (-not $sidecarPathOk) { throw 'installed sidecar is not running from the install resources' }

    $before = @{
        apps = Count-Process 'quintessence-desktop'
        sidecars = Count-Process 'pcs-bridge'
    }
    $previousPath = $env:PATH
    $previousConfig = $env:QUINTESSENCE_DESKTOP_CONFIG
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $env:QUINTESSENCE_DESKTOP_CONFIG = $config
    try {
        $second = Start-Process -FilePath (Get-InstalledApp).FullName -PassThru
    }
    finally {
        $env:PATH = $previousPath
        if ($null -eq $previousConfig) { Remove-Item Env:\QUINTESSENCE_DESKTOP_CONFIG -ErrorAction SilentlyContinue }
        else { $env:QUINTESSENCE_DESKTOP_CONFIG = $previousConfig }
    }
    $second.WaitForExit(15000) | Out-Null
    Start-Sleep -Seconds 1
    $singleInstance = [ordered]@{
        second_exit_code = if ($second.HasExited) { $second.ExitCode } else { $null }
        second_still_running = -not $second.HasExited
        app_processes = Count-Process 'quintessence-desktop'
        sidecar_processes = Count-Process 'pcs-bridge'
        passed = ($second.HasExited -and (Count-Process 'quintessence-desktop') -eq 1 -and (Count-Process 'pcs-bridge') -eq 1)
    }
    if (-not $singleInstance.passed) {
        if (-not $second.HasExited) { Stop-Process -Id $second.Id -Force }
    }
    $singleInstance | ConvertTo-Json | Set-Content -Path (Join-Path $Evidence 'single-instance.json') -Encoding utf8

    $installedSidecarExe = Join-Path $InstallDir 'resources\sidecar\pcs-bridge\pcs-bridge.exe'
    & python (Join-Path $AppRoot 'scripts\probe_lock.py') --exe $installedSidecarExe --config $config |
        Tee-Object -FilePath (Join-Path $Evidence 'data-lock.log')
    if ($LASTEXITCODE -ne 0) { throw 'direct second sidecar did not report DATA_LOCKED' }
    $dataLock = Get-Content (Join-Path $Evidence 'data-lock.log') -Raw | ConvertFrom-Json

    $scenarioResults['answered'] = [ordered]@{
        screenshots = 'evidence/installed/answered'
        store = 'evidence/store-answered.json'
        sidecar_path = $sidecarProcess.ExecutablePath
        sidecar_path_ok = $sidecarPathOk
    }
}
finally {
    Stop-App
}

# --- other result states, one fresh app process per scripted scenario.
foreach ($entry in @(
    @{ tag = 'waiting'; mode = 'waiting'; expect = 'waiting' },
    @{ tag = 'failed'; mode = 'failed'; expect = 'failed' },
    @{ tag = 'incomplete'; mode = 'incomplete'; expect = 'incomplete' }
)) {
    $config = Write-ScenarioConfig $entry.tag $entry.mode
    $app = Start-AppWithConfig $config $entry.tag
    try {
        Wait-Cdp
        Invoke-Capture $entry.tag '1280x800' $entry.tag
        Invoke-StoreCheck $entry.tag $entry.expect
        $scenarioResults[$entry.tag] = [ordered]@{
            screenshots = "evidence/installed/$($entry.tag)"
            store = "evidence/store-$($entry.tag).json"
        }
    }
    finally {
        Stop-App
    }
}

# --- confirmation banner: no consult is sent before the user confirms.
$config = Write-ScenarioConfig 'confirm' 'answered'
$app = Start-AppWithConfig $config 'confirm'
try {
    Wait-Cdp
    Invoke-Capture 'confirm' '1280x800' 'confirm'
    $scenarioResults['confirm'] = [ordered]@{ screenshots = 'evidence/installed/confirm' }
}
finally {
    Stop-App
}

# --- Review placeholder: no model call, no candidate action.
$config = Write-ScenarioConfig 'review' 'answered'
$app = Start-AppWithConfig $config 'review'
try {
    Wait-Cdp
    Invoke-Capture 'review' '1280x800' 'review'
    $scenarioResults['review'] = [ordered]@{ screenshots = 'evidence/installed/review' }
}
finally {
    Stop-App
}

# --- not-ready host: a missing private configuration disables sending.
$config = Write-ScenarioConfig 'not-ready' 'answered' -Missing
$app = Start-AppWithConfig $config 'not-ready'
try {
    Wait-Cdp
    Invoke-Capture 'not-ready' '1280x800' 'not-ready'
    $scenarioResults['not-ready'] = [ordered]@{ screenshots = 'evidence/installed/not-ready' }
}
finally {
    Stop-App
}

# --- high DPI (200%) and Chinese IME composition, 880x560 logical.
$config = Write-ScenarioConfig 'hidpi' 'answered'
$app = Start-AppWithConfig $config 'hidpi' -HighDpi
try {
    Wait-Cdp
    Invoke-Capture 'input' '880x560' 'hidpi-input' '2'
    Invoke-Capture 'ime' '880x560' 'hidpi-ime' '2'
    $hidpiState = Get-Content (Join-Path $Shots 'hidpi-input\input-state.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $imeState = Get-Content (Join-Path $Shots 'hidpi-ime\ime-state.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $hidpi = [ordered]@{
        dpr = $hidpiState.state.viewport.dpr
        viewport = $hidpiState.state.viewport
        screenshot = 'evidence/installed/hidpi-input/input-880x560.png'
        ime_screenshot = 'evidence/installed/hidpi-ime/ime-880x560.png'
        ime_guard = $imeState.state.imeGuard
        passed = (
            $hidpiState.state.viewport.dpr -eq 2 -and
            $imeState.state.imeGuard -eq 'true' -and
            $hidpiState.state.viewport.width -lt 900
        )
    }
    if (-not $hidpi.passed) { throw 'high DPI or IME evidence failed' }
    $scenarioResults['hidpi'] = $hidpi
}
finally {
    Stop-App
}

# --- scans before uninstall
$installedSidecarDir = Join-Path $InstallDir 'resources\sidecar\pcs-bridge'
& python (Join-Path $AppRoot 'scripts\scan_private.py') `
    --root $Installer --root $InstallDir --root (Join-Path $InstallDir 'quintessence-desktop.exe') `
    --root $installedSidecarDir --output (Join-Path $Evidence 'scan-install.json') --json
if ($LASTEXITCODE -ne 0) { throw 'private-data scan failed' }

# --- uninstall
$uninstaller = Get-ChildItem $InstallDir -Filter 'uninstall.exe' -File -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($null -eq $uninstaller) { throw 'uninstaller not found' }
$uninstall = Start-Process -FilePath $uninstaller.FullName -ArgumentList @('/S') -Wait -PassThru
Start-Sleep -Seconds 2
$installRemoved = -not (Test-Path $InstallDir)
$databaseRetained = Test-Path (Get-Database 'answered')

$summary = [ordered]@{
    installer = $Installer
    installer_sha256 = (Get-FileHash $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
    installer_bytes = (Get-Item $Installer).Length
    installed_tree = $tree
    uninstall_exit_code = $uninstall.ExitCode
    install_dir_removed = $installRemoved
    database_retained = $databaseRetained
    single_instance = $singleInstance
    data_lock = $dataLock
    high_dpi = $scenarioResults['hidpi']
    scenarios = $scenarioResults
    passed = (
        $tree.passed -and
        $singleInstance.passed -and
        $dataLock.passed -and
        $scenarioResults['hidpi'].passed -and
        $installRemoved -and
        $databaseRetained
    )
}
$summary | ConvertTo-Json -Depth 8 | Set-Content -Path (Join-Path $Evidence 'install-evidence.json') -Encoding utf8
$summary | ConvertTo-Json -Depth 8
if (-not $summary.passed) { exit 1 }
