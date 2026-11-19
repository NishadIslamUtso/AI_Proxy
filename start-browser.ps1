# Launch the browser the proxy drives, on the OWNER's Brave profile
# (through a space-free junction) so the brain uses his existing logins.
# Hermes's own browser uses a separate dedicated profile, so they never fight.
$brave = "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
if (-not (Test-Path $brave)) {
  $brave = "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\Application\brave.exe"
}
if (-not (Test-Path $brave)) { Write-Output "Brave not found"; exit 1 }

$src  = "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\User Data"
$link = "C:\hermes\brave-userdata"
if (-not (Test-Path $link)) {
  New-Item -ItemType Junction -Path $link -Target $src | Out-Null
}

# stop anything already holding either profile
Get-CimInstance Win32_Process -Filter "name='brave.exe'" |
  Where-Object { $_.CommandLine -like '*brave-userdata*' -or $_.CommandLine -like '*brain-proxy-profile*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3

Start-Process -FilePath $brave -ArgumentList @(
  "--user-data-dir=$link",
  "--remote-debugging-port=9223",
  "--remote-allow-origins=*",
  "--no-first-run",
  "--no-default-browser-check",
  "about:blank"
)
Write-Output "proxy browser launched on 9223"
