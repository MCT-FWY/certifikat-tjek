@echo off
REM Starter certifikat-dashboardet. Luk vinduet for at stoppe.
cd /d "%~dp0"
REM Installer requests foerste gang (bruges af selve tjekket)
python -c "import requests" 2>NUL || python -m pip install -r requirements.txt
python server.py %*
pause
