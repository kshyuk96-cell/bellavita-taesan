@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0diagnose.ps1" goto missing
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0diagnose.ps1"
if errorlevel 1 echo Diagnostic did not finish. Keep this window open and check the report.
echo.
echo Diagnostic finished. No chat was sent. Press any key to close this console.
pause >nul
exit /b
:missing
echo Extract the whole ZIP before running START_DIAGNOSE.cmd.
pause
exit /b 1
