# Launch Hermes's own browser on a dedicated profile (CDP 9222),
# so it never fights the proxy's browser on 9223.
$brave = "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\Application\brave.exe"
if (-not (Test-Path $brave)) {
  $brave = "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
}
if (-not (Test-Path $brave)) { Write-Output "Brave not found"; exit 1 }

$dir = "C:\hermes\brave-profile"
New-Item -ItemType Directory -Force -Path $dir | Out-Null

Get-CimInstance Win32_Process -Filter "name='brave.exe'" |
  Where-Object { $_.CommandLine -like '*brave-profile*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

Start-Process -FilePath $brave -ArgumentList @(
  "--user-data-dir=$dir",
  "--no-first-run",
  "--no-default-browser-check",
  "about:blank"
)
Write-Output "hermes browser launched on 9222"
