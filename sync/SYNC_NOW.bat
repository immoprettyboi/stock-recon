@echo off
cd /d "%~dp0"
echo Running one sync now (Chrome opens so you can watch)...
py stockrecon_sync.py --once --show || python stockrecon_sync.py --once --show
echo.
echo Done. See sync_log.txt for details.
pause
