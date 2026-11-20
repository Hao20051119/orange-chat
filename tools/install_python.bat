@echo off
rem 自动补全 Python：下载 64 位 Python 3.12 完整安装包并静默装到用户目录。
rem 为什么不用 embed 版：embeddable 包不带 tkinter，而 GUI 界面需要 tkinter。
rem 由 python_path.bat 探测失败后调用，成功后写 <项目根>\python_path.txt。

setlocal
set "TARGET=%LOCALAPPDATA%\Orange\python312"
set "EXE=%TEMP%\python-3.12.7-amd64.exe"
set "URL1=https://mirrors.huaweicloud.com/python/3.12.7/python-3.12.7-amd64.exe"
set "URL2=https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"

if exist "%TARGET%\python.exe" goto verify

echo.
echo ============================================================
echo  没检测到可用的 Python 3.12（64 位）。
echo  现在自动下载官方安装包（约 25 MB）并装到：
echo    %TARGET%
echo  只装给当前用户，不改系统 PATH，不需要管理员权限。
echo ============================================================
echo.
set "AGREE="
set /p AGREE=继续吗？输入 Y 回车开始，直接回车取消：
if /i not "%AGREE%"=="Y" (
    echo 已取消。
    exit /b 1
)

echo.
echo [1/3] 下载安装包（先试华为云镜像）...
curl.exe -L --fail --connect-timeout 15 --retry 2 -sS -o "%EXE%" "%URL1%"
if errorlevel 1 (
    echo       镜像失败，改用 python.org 官方源...
    curl.exe -L --fail --connect-timeout 15 --retry 2 -sS -o "%EXE%" "%URL2%"
)
if not exist "%EXE%" goto dl_failed
for %%F in ("%EXE%") do if %%~zF LSS 20000000 goto dl_failed

echo [2/3] 静默安装（大约 1-2 分钟，请不要关窗口）...
"%EXE%" /quiet InstallAllUsers=0 PrependPath=0 Include_test=0 ^
 Include_launcher=0 Include_doc=0 Include_tcltk=1 Include_pip=1 ^
 TargetDir="%TARGET%" /log "%TEMP%\python312_install.log"
if not exist "%TARGET%\python.exe" goto inst_failed

:verify
echo [3/3] 检查版本与 tkinter...
"%TARGET%\python.exe" -c "import sys;sys.exit(0 if sys.version_info[:2]==(3,12) and sys.maxsize>2**32 else 1)"
if errorlevel 1 goto ver_failed
"%TARGET%\python.exe" -c "import tkinter" 2>nul
if errorlevel 1 goto tk_failed

echo       补装知识库解析包（docx/pdf/xlsx/pptx，失败不影响聊天）...
"%TARGET%\python.exe" -m pip install --quiet --disable-pip-version-check ^
 python-docx pypdf openpyxl python-pptx

>"%~dp0..\python_path.txt" (echo %TARGET%\python.exe)
echo.
echo 完成：已装好 Python 并写入 python_path.txt
echo   %TARGET%\python.exe
echo.
del /q "%EXE%" 2>nul
endlocal
exit /b 0

:dl_failed
echo.
echo [失败] 安装包下载不下来（没有网络或被安全软件拦了）。
echo        可以手动装：https://www.python.org/downloads/windows/
echo        装 64 位 3.12，然后双击「安装.bat」指过去。
endlocal
exit /b 1

:inst_failed
echo.
echo [失败] 静默安装没成功，日志：%TEMP%\python312_install.log
echo        可以手动双击 %EXE% 安装，勾选 tcl/tk 和 pip，
echo        装完双击「安装.bat」指过去。
endlocal
exit /b 1

:ver_failed
echo.
echo [失败] 装出来的不是 3.12 64 位（可能目标目录里是旧版本）。
echo        删掉 %TARGET% 再重跑一次。
endlocal
exit /b 1

:tk_failed
echo.
echo [失败] 这个 Python 缺少 tkinter（GUI 需要它）。
echo        手动重装时请勾选 "tcl/tk and IDLE"。
endlocal
exit /b 1
