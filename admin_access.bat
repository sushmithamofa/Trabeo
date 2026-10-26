@echo off
rem CAD to Revit - who may use the app. For the admin only: approve, renew or remove users.
rem It needs the admin's signing key (made by "Setup", kept in %USERPROFILE%\.c2r_admin).
cd /d "%~dp0"
python tools\access_admin.py
pause
