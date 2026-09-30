@echo off
cd /d "%~dp0"
set "TSI_GITHUB_REPOSITORY=michaelagana20/treasurers-supply-inventory"
py -3 -c "import sys" >nul 2>nul
if %errorlevel% equ 0 (
  py -3 server.py
) else (
  python server.py
)
