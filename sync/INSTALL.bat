@echo off
cd /d "%~dp0"
echo ============================================
echo   Stock Recon - Takealot sync: install
echo ============================================
echo.
echo Installing Playwright (lets the sync control Chrome)...
py -m pip install --upgrade playwright
if errorlevel 1 python -m pip install --upgrade playwright
echo.
choice /m "Start the sync automatically when Windows starts"
if errorlevel 2 goto skip
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\Stock Recon Sync.lnk'); $s.TargetPath='%~dp0START.bat'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Save()"
echo Added to Windows startup.
:skip
echo.
echo Next:
echo   1. Open config.ini in Notepad and paste your Supabase service key
echo   2. Double-click LOGIN.bat and log into the Takealot Seller Portal
echo   3. Double-click SYNC_NOW.bat to test, then START.bat
echo.
pause
