param(
    [int]$Width = 1280,
    [int]$Height = 800
)

$ErrorActionPreference = 'Stop'
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class QuintessenceWindow {
    [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr hWnd, int x, int y, int width, int height, bool repaint);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
"@

$process = Get-Process -Name 'quintessence-desktop' -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 } |
    Select-Object -First 1
if ($null -eq $process) {
    $process = Get-Process |
        Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -eq 'QUINTESSENCE · Consult' } |
        Select-Object -First 1
}
if ($null -eq $process) {
    throw 'QUINTESSENCE window not found'
}
[QuintessenceWindow]::ShowWindow($process.MainWindowHandle, 9) | Out-Null
[QuintessenceWindow]::MoveWindow($process.MainWindowHandle, 30, 30, $Width, $Height, $true) | Out-Null
[QuintessenceWindow]::SetForegroundWindow($process.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 350
Write-Output ([ordered]@{ pid = $process.Id; width = $Width; height = $Height } | ConvertTo-Json -Compress)
