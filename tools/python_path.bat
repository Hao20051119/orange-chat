@echo off
rem 统一探测 Python：其他 .bat 用 call 调用本文件，读完取 PY / PYW 两个变量。
rem 要求：64 位 Python 3.12。会跳过 Windows 应用商店的占位 python.exe。
if not defined WXBOT_HOME set "WXBOT_HOME=%~dp0.."
set "PY="
set "PYW="

rem 1) 用户手动指定（python_path.txt / 安装.bat 写入）
if exist "%WXBOT_HOME%\python_path.txt" (
    for /f "usebackq delims=" %%P in ("%WXBOT_HOME%\python_path.txt") do (
        if not defined PY call :accept "%%~P"
    )
)

rem 2) 常见安装位置（Anaconda / Miniconda / 官方安装包）
if not defined PY call :accept "%LOCALAPPDATA%\Orange\python312\python.exe"
if not defined PY call :accept "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY call :accept "%USERPROFILE%\anaconda3\python.exe"
if not defined PY call :accept "%USERPROFILE%\miniconda3\python.exe"
if not defined PY call :accept "C:\ProgramData\Anaconda3\python.exe"
if not defined PY call :accept "C:\ProgramData\miniconda3\python.exe"
if not defined PY call :accept "D:\Anaconda3\python.exe"
if not defined PY call :accept "D:\Miniconda3\python.exe"
if not defined PY call :accept "C:\Python312\python.exe"

rem 3) PATH 里找（:accept 会过滤掉 WindowsApps 占位程序和非 3.12）
if not defined PY (
    for /f "delims=" %%P in ('where python.exe 2^>nul') do (
        if not defined PY call :accept "%%~fP"
    )
)

if not defined PY exit /b 1
for %%D in ("%PY%") do set "PYW=%%~dpDpythonw.exe"
if not exist "%PYW%" set "PYW=%PY%"
exit /b 0

:accept
rem %1 = 候选路径。三条判据缺一不可：
rem   1) 不在 WindowsApps 下（应用商店占位程序，运行只会弹商店）
rem   2) 真的能执行并打印 OK312（过滤 cmd.exe 这类能执行但不是 Python 的）
rem   3) 恰好 3.12 且 64 位（依赖里的 .pyd 是 cp312-win_amd64）
set "CAND=%~1"
if not defined CAND exit /b 1
echo %CAND% | findstr /I /C:"\WindowsApps\" >nul && exit /b 1
if not exist "%CAND%" exit /b 1
set "PYVERFILE=%TEMP%\orange_pyver.txt"
del "%PYVERFILE%" >nul 2>&1
"%CAND%" -c "import sys;print('OK312' if sys.version_info[:2]==(3,12) and sys.maxsize>2**32 else 'NO')" >"%PYVERFILE%" 2>nul
set "VER="
if exist "%PYVERFILE%" for /f "usebackq delims=" %%V in ("%PYVERFILE%") do set "VER=%%V"
del "%PYVERFILE%" >nul 2>&1
if not "%VER%"=="OK312" exit /b 1
set "PY=%CAND%"
exit /b 0
