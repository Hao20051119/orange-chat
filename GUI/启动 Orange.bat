@echo off
setlocal
set "WXBOT_HOME=%~dp0..\"
call "%WXBOT_HOME%tools\python_path.bat"
set "ORANGE_PYTHON=%WXBOT_PYTHON%"
if defined ORANGE_PYTHON goto launch
if defined PYW set "ORANGE_PYTHON=%PYW%"
if defined ORANGE_PYTHON goto launch
for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do if not defined ORANGE_PYTHON set "ORANGE_PYTHON=%%P"

:launch
if not defined ORANGE_PYTHON goto missing_python
if not exist "%ORANGE_PYTHON%" goto missing_python
if not exist "%~dp0launch.py" goto missing_launcher
start "" /d "%~dp0." "%ORANGE_PYTHON%" "%~dp0launch.py"
if not errorlevel 1 exit /b 0
echo Orange could not start the Python process.
goto failed

:missing_python
echo Python was not found. Set WXBOT_PYTHON to the path of pythonw.exe.
goto failed

:missing_launcher
echo Orange launcher was not found. Keep all files in the GUI folder together.

:failed
pause
exit /b 1
