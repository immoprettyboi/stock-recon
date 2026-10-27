@echo off
cd /d "%~dp0"
rem Runs the sync in the background every 30 minutes (no black window)
where pyw >nul 2>nul && (start "" pyw stockrecon_sync.py & exit /b)
start "" pythonw stockrecon_sync.py
exit /b
