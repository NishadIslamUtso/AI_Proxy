@echo off
cd /d C:\hermes\brain-proxy
echo starting brain-proxy...
.venv\Scripts\python.exe server.py >> brain-proxy.log 2>&1
