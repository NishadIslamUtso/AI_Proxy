@echo off
cd /d C:\hermes\brain-proxy
echo [%date% %time%] starting brain-proxy >> brain-proxy.log
.venv\Scripts\python.exe server.py >> brain-proxy.log 2>&1
