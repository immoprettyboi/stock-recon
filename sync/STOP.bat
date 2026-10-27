@echo off
rem Stops the background sync
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*stockrecon_sync.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Stock Recon sync stopped.
pause
