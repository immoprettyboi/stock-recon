@echo off
cd /d "%~dp0"
echo Chrome will open on the Takealot Seller Portal.
echo Log in there. The window closes by itself once you're in.
py stockrecon_sync.py --login || python stockrecon_sync.py --login
pause
