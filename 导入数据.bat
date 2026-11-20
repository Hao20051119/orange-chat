@echo off
chcp 936 >nul
title Orange - 导入数据 / 就地适配
cd /d "%~dp0"
set "PYTHONIOENCODING=gbk"
set "WXBOT_HOME=%~dp0"

echo ============================================================
echo  数据搬家：固定人设 / 关系 / 名单 / 记忆 / 定时提醒 / 知识库 / 表情包
echo.
echo  方式一（推荐）：把旧电脑导出的 zip 放进本文件夹（或拖到本文件上）。
echo  方式二：整个文件夹拷过来的（没导出过 zip），直接双击本文件，
echo          会按「就地适配」修正微信账号目录等本机差异。
echo ============================================================
echo.

call "%WXBOT_HOME%tools\python_path.bat"
if not defined PY goto no_python

rem 找数据包：拖拽参数 → Orange数据*.zip → 迁移包.zip → 任意 zip
set "PKG=%~1"
if not defined PKG for %%Z in ("%~dp0Orange数据*.zip") do if not defined PKG set "PKG=%%~fZ"
if not defined PKG if exist "%~dp0迁移包.zip" set "PKG=%~dp0迁移包.zip"
if not defined PKG for %%Z in ("%~dp0*.zip") do if not defined PKG set "PKG=%%~fZ"
if defined PKG (echo 数据包：%PKG%) else (echo 没找到数据包，按「就地适配」处理当前文件夹里的旧数据)
echo.

echo [1/3] 自检（依赖 / 微信 / 版本 / 小号）
echo ------------------------------------------------------------
"%PY%" tools\migrate_device.py doctor
if errorlevel 1 goto check_failed

echo.
echo [2/3] 预演：只打印，不改任何东西
echo ------------------------------------------------------------
if defined PKG ("%PY%" tools\migrate_device.py import "%PKG%") else ("%PY%" tools\migrate_device.py adapt)
if errorlevel 1 goto preview_failed

echo.
set "ANSWER="
set /p ANSWER=确认写入？输入 Y 回车执行，直接回车取消：
if /i not "%ANSWER%"=="Y" goto cancelled

echo.
echo [3/3] 写入（会先备份目标目录）
echo ------------------------------------------------------------
if defined PKG ("%PY%" tools\migrate_device.py import "%PKG%" --apply) else ("%PY%" tools\migrate_device.py adapt --apply)
if errorlevel 1 goto apply_failed

echo.
echo ============================================================
echo  完成。下一步：
echo    1. 双击 GUI\启动 Orange.bat
echo    2. 选小号 - 勾选确认 - 选模型 API（首次填 Key）
echo    3. 点「连接并启动」
echo    4. 面板里打开【自动回复】【主动聊天】【定时提醒】
echo ============================================================
echo.
echo 建议：确认数据正常后，把这个 zip 删掉或移走。
echo.
pause
exit /b 0

:no_python
echo.
echo [失败] 没找到可用的 Python 3.12。
echo        先跑一遍「安装.bat」，再回来。
echo.
pause
exit /b 1

:check_failed
echo.
echo [!] 自检没通过。最常见的原因：
echo      - 微信还没登录小号（先登录，保持微信运行）
echo      - Python 版本不是 3.12 64 位
echo.
pause
exit /b 1

:preview_failed
echo.
echo [!] 预演失败，没改任何东西。看上面的报错。
echo.
pause
exit /b 1

:apply_failed
echo.
echo [!] 写入失败，数据没有改动（写入前会自动备份目标目录）。
echo.
pause
exit /b 1

:cancelled
echo.
echo 已取消，什么都没改。
echo.
pause
exit /b 0
