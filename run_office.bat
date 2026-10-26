@echo off
rem CAD to Revit - office mode. Colleagues on the office network use the app on THIS PC
rem through their own browser: nothing is copied to their PCs. They sign in with their
rem approved e-mail and a password; you approve them on the Users page here.
rem Needs once: admin_access.bat choice 9 (the e-mail account the app sends links from).
rem Closing the small "CAD to Revit office" window stops it.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on this machine.
  pause
  exit /b 1
)

for %%M in (ezdxf shapely openpyxl) do (
  python -c "import %%M" >nul 2>nul
  if errorlevel 1 (
    echo Installing %%M ...
    python -m pip install --quiet %%M
  )
)

python -c "import PIL" >nul 2>nul
if errorlevel 1 python -m pip install --quiet pillow >nul 2>nul

title CAD to Revit office
python webapp.py --office
pause
