@echo off
rem The sync now runs on the server: stop it on this PC and stop it starting with Windows.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*stockrecon_sync.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Stock Recon Sync.lnk" 2>nul
echo.
echo Stock Recon is stopped on this PC and won't start with Windows any more.
echo It now runs on the server. (To run it on this PC again: double-click START.bat)
pause
