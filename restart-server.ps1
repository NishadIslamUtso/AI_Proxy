# Kill whatever owns port 8000, then start exactly one fresh server.
$conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($conn) {
  $owner = $conn | Select-Object -First 1 -ExpandProperty OwningProcess
  Write-Output "killing port-8000 owner: $owner"
  Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process -Filter "name='python.exe'" |
  Where-Object { $_.CommandLine -like '*server.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3

Start-Process -FilePath "C:\hermes\brain-proxy\.venv\Scripts\python.exe" `
  -ArgumentList "server.py" `
  -WorkingDirectory "C:\hermes\brain-proxy" `
  -RedirectStandardOutput "C:\hermes\brain-proxy\out.log" `
  -RedirectStandardError "C:\hermes\brain-proxy\err.log" `
  -WindowStyle Hidden
Start-Sleep -Seconds 8
$conn2 = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($conn2) {
  $new = $conn2 | Select-Object -First 1 -ExpandProperty OwningProcess
  $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$new"
  Write-Output "listening pid=$new started=$($proc.CreationDate)"
} else {
  Write-Output "NOT LISTENING"
}
