@echo off
chcp 936 >nul
title Orange - 导出数据（旧电脑用）
cd /d "%~dp0"
set "PYTHONIOENCODING=gbk"
set "WXBOT_HOME=%~dp0"

echo ============================================================
echo  把当前电脑上的数据打包成一个压缩包：
echo    固定人设 / 关系 / 名单 / 记忆 / 定时提醒 / 知识库 / 表情包
echo.
echo  不会打包：API Key、微信聊天记录、日志缓存
echo ============================================================
echo.

call "%WXBOT_HOME%tools\python_path.bat"
if not defined PY goto no_python

echo 先看一眼现在有多少数据：
echo ------------------------------------------------------------
"%PY%" tools\migrate_device.py status
if errorlevel 1 goto no_data

echo.
echo 建议先退出微信再打包（运行中也能打，但退出后数据库更干净）。
echo.
set "ANSWER="
set /p ANSWER=开始打包？输入 Y 回车执行，直接回车取消：
if /i not "%ANSWER%"=="Y" goto cancelled

echo.
echo 正在打包...
echo ------------------------------------------------------------
"%PY%" tools\migrate_device.py export
if errorlevel 1 goto failed

echo.
echo ============================================================
echo  打包完成。把上面那个 zip 拷到另一台电脑的 Orange 文件夹里，
echo  然后双击那边的「导入数据.bat」。
echo ============================================================
echo.
pause
exit /b 0

:no_python
echo.
echo [失败] 没找到可用的 Python 3.12。
echo        这台电脑如果还没装环境，先跑一遍「安装.bat」。
echo.
pause
exit /b 1

:no_data
echo.
echo [失败] 没读到本机的 Orange 数据（可能还没在这台电脑上跑过）。
echo.
pause
exit /b 1

:failed
echo.
echo [失败] 打包没成功，把上面的报错发出来。
echo.
pause
exit /b 1

:cancelled
echo.
echo 已取消，什么都没做。
echo.
pause
exit /b 0
