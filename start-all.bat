@echo off
cd /d C:\hermes\brain-proxy
echo [%date% %time%] starting brain-proxy >> brain-proxy.log
powershell -NoProfile -ExecutionPolicy Bypass -File start-browser.ps1 >> brain-proxy.log 2>&1
.venv\Scripts\python.exe server.py >> brain-proxy.log 2>&1
