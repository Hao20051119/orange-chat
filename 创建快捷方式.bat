@echo off
rem ASCII-only on purpose: cmd parses .bat bytes with the console code page,
rem so Chinese text here would break on machines whose code page is not 936.
rem Chinese output all comes from tools\make_shortcut.ps1 (UTF-8 with BOM).
title Orange - Create Shortcut
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\make_shortcut.ps1"
if errorlevel 1 goto failed

echo.
pause
exit /b 0

:failed
echo.
echo [FAILED] shortcut not created, see the message above.
echo.
pause
exit /b 1
