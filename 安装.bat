@echo off
chcp 936 >nul
title 微信小号接入 DeepSeek - 安装
cd /d "%~dp0"
set "PYTHONIOENCODING=gbk"
set "WXBOT_HOME=%~dp0"

echo ============================================================
echo  微信小号接入 DeepSeek - 安装向导
echo  需要：Windows 10/11，微信 4.1.12+ 且已登录
echo  本脚本会自动：装 Python（缺的话）- 装依赖 - 生成配置 - 自检
echo ============================================================
echo.

echo [1/4] 检查 Python（需要 64 位 3.12）
echo ------------------------------------------------------------
call "%WXBOT_HOME%tools\python_path.bat"
if defined PY goto have_python
echo 没找到可用的 Python，尝试自动安装...
call "%WXBOT_HOME%tools\install_python.bat"
call "%WXBOT_HOME%tools\python_path.bat"
if not defined PY goto no_python
:have_python
echo 使用：%PY%
echo.

echo [2/4] 安装依赖（首次约 3-5 分钟，看到进度条别关窗口）
echo ------------------------------------------------------------
"%PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo.
    echo 默认源太慢或失败，改用清华镜像重试...
    "%PY%" -m pip install --disable-pip-version-check -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
)
if errorlevel 1 goto pip_failed

echo.
echo [3/4] 生成配置文件（已存在的不会覆盖）
echo ------------------------------------------------------------
if exist "config.json" (
    echo   config.json 已存在，保留你的设置
) else (
    copy "config.example.json" "config.json" >nul
    echo   config.json 已生成
)
if exist "persona_card.json" (
    echo   persona_card.json 已存在，保留你的人设
) else (
    copy "persona_card.example.json" "persona_card.json" >nul
    echo   persona_card.json 已生成（里面是占位人设，记得改）
)
echo.

echo [4/4] 自检
echo ------------------------------------------------------------
"%PY%" tools\migrate_device.py doctor
if errorlevel 1 goto check_failed

echo.
echo ============================================================
echo  安装完成，下一步：
echo    1. 双击 GUI\启动 Orange.bat
echo    2. 选小号 - 勾选确认 - 选模型 API（第一次要填 Key）
echo    3. 点「连接并启动」
echo    4. 在面板里打开【自动回复】
echo.
echo  改人设：直接编辑 persona_card.json，
echo          或启动后在 GUI 的「固定人设」「关系」页里改。
echo ============================================================
echo.
pause
exit /b 0

:no_python
echo.
echo [失败] 没有可用的 Python 3.12（64 位）。
echo.
echo 当前 PATH 里的 python：
where python 2^>nul
echo.
echo 注意：如果上面出现 ...\WindowsApps\python.exe，那是 Windows 应用商店
echo       的占位程序，不是真的 Python，运行它只会弹出商店。
echo.
echo 三个办法（任选一个）：
echo   1) 重跑本脚本，选「自动下载安装 Python 3.12」（最简单）
echo   2) 已装 Anaconda：conda create -n py312 python=3.12
echo   3) 直接在下面粘贴已有 python.exe 的完整路径（会写进 python_path.txt）
echo.
set "P="
set /p P=python.exe 路径（回车跳过）：
set "P=%P:"=%"
if defined P (
    "%P%" -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo [!] 这个路径不能运行 Python（或者不是 64 位），检查一下再重试。
    ) else (
        >"%~dp0python_path.txt" (echo %P%)
        echo [OK] 已写入 python_path.txt，重新运行「安装.bat」继续装依赖。
    )
)
echo.
pause
exit /b 1

:pip_failed
echo.
echo [失败] 依赖没装上（网络或权限问题）。
echo        手动试： "%PY%" -m pip install -r requirements.txt
echo        公司/校园网可能有代理，设置 HTTP_PROXY 后重试。
echo.
pause
exit /b 1

:check_failed
echo.
echo [!] 自检有项目没通过，按上面标 [缺] 的提示处理。
echo.
pause
exit /b 1
