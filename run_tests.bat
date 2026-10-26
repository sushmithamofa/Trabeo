@echo off
rem Runs every test suite and every locked baseline, and prints PASS / FAIL for each.
rem About 5 minutes. Library warnings are hidden: only the table at the end matters.
cd /d "%~dp0"
python tools\run_all_tests.py --baselines
echo.
pause
