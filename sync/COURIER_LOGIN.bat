@echo off
cd /d "%~dp0"
echo Chrome will open on the Courier Guy portal.
echo Log in there. The window closes by itself once you are in.
py courier_login.py || python courier_login.py
pause
