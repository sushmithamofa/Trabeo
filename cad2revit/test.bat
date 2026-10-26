@echo off
rem Checks the tool is working correctly. Double-click this file.
cd /d "%~dp0"

echo.
echo Checking the CAD to Revit extractor...
echo.

python selftest.py
set RESULT=%ERRORLEVEL%

echo.
if "%RESULT%"=="0" (
  echo ================================================
  echo   ALL TESTS PASSED - the tool is working.
  echo ================================================
) else (
  echo ================================================
  echo   SOMETHING FAILED - read the FAIL lines above.
  echo   Do not trust the output until they pass.
  echo ================================================
)
echo.
pause
