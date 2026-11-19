$b = @{ model = "arena-agent"; stream = $true; messages = @(@{ role = "user"; content = "Reply with exactly: STREAM OK" }) } | ConvertTo-Json -Depth 5 -Compress
$req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:8000/v1/chat/completions")
$req.Method = "POST"
$req.ContentType = "application/json"
$req.Timeout = 900000
$bytes = [System.Text.Encoding]::UTF8.GetBytes($b)
$req.ContentLength = $bytes.Length
$s = $req.GetRequestStream()
$s.Write($bytes, 0, $bytes.Length)
$s.Close()
$resp = $req.GetResponse()
$sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
$txt = $sr.ReadToEnd()
$sr.Close()
$lines = $txt -split "`n"
Write-Output ("lines: " + $lines.Count)
$lines | Where-Object { $_ -match 'STREAM OK' -or $_ -match 'error' -or $_ -match 'DONE' } | Select-Object -First 6
