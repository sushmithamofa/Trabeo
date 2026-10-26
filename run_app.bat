@echo off
rem CAD to Revit - import a job's drawings, analyse, review (plan and 3D), export to Revit.
rem Double-click this file: the app opens in your browser. It runs on this PC only.
rem Closing the small "CAD to Revit" window stops the app.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on this machine.
  echo Install Python 3.10 or later from python.org, tick "Add python.exe to PATH",
  echo then run this file again.
  pause
  exit /b 1
)

for %%M in (ezdxf shapely openpyxl) do (
  python -c "import %%M" >nul 2>nul
  if errorlevel 1 (
    echo Installing %%M ...
    python -m pip install --quiet %%M
    if errorlevel 1 (
      echo Could not install %%M. With no internet, run:  python -m pip install %%M
      pause
      exit /b 1
    )
  )
)

rem pictures in the consultant's Excel (optional: without it the screenshots go in the PDF only)
python -c "import PIL" >nul 2>nul
if errorlevel 1 python -m pip install --quiet pillow >nul 2>nul

start "CAD to Revit" /min python webapp.py
