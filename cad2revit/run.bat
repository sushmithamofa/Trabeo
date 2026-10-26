@echo off
rem Launches the CAD to Revit extractor. Double-click this file.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on this machine.
  echo Install Python 3.10 or later from python.org, tick "Add python.exe to PATH",
  echo then run this file again.
  pause
  exit /b 1
)

python -c "import ezdxf" >nul 2>nul
if errorlevel 1 (
  echo Installing ezdxf, the DXF library this tool needs...
  python -m pip install --quiet ezdxf
  if errorlevel 1 (
    echo Could not install ezdxf. With no internet, run:  python -m pip install ezdxf
    pause
    exit /b 1
  )
)

python -c "import shapely" >nul 2>nul
if errorlevel 1 (
  echo Installing shapely, used to find the slab panels between the beams...
  python -m pip install --quiet shapely
  if errorlevel 1 (
    echo Could not install shapely. Slabs will not be found until it is. Run:  python -m pip install shapely
    pause
  )
)

start "" pythonw app.py
