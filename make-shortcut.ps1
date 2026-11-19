$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup 'brain-proxy.lnk'
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = 'C:\hermes\brain-proxy\start-all.bat'
$sc.WorkingDirectory = 'C:\hermes\brain-proxy'
$sc.WindowStyle = 7
$sc.Save()
Write-Output "shortcut: $lnk"
