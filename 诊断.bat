@echo off
title 微信小号 - 诊断
cd /d "%~dp0"
echo ==== 1. Python ====
set "WXBOT_HOME=%~dp0"
call "%WXBOT_HOME%tools\python_path.bat"
if defined PY (echo OK: %PY%) else (echo 缺少 Python - 需要 64 位 3.12，双击「安装.bat」)
echo.
echo ==== 2. 关键文件 ====
if exist "wxbot\wx_service.py" (echo OK: wxbot\wx_service.py) else (echo 缺少 wxbot\wx_service.py)
if exist "agent\reply_worker.py" (echo OK: agent\reply_worker.py) else (echo 缺少 agent\reply_worker.py)
if exist "GUI\启动 Orange.bat" (echo OK: GUI\启动 Orange.bat) else (echo 缺少 GUI\启动 Orange.bat)
echo.
echo ==== 3. 微信是否在运行 ====
tasklist /fi "imagename eq Weixin.exe" | find /i "Weixin.exe" >nul
if errorlevel 1 (echo 微信未运行：请先登录小号) else (echo 微信正在运行)
echo.
echo ==== 4. 端口 8765 ====
netstat -ano | findstr ":8765" | findstr LISTENING
if errorlevel 1 (echo 8765 没有监听：服务未启动或端口被改) else (echo 8765 已在监听)
echo.
echo ==== 5. python 进程 ====
tasklist /fi "imagename eq python.exe" | find /i "python.exe"
if errorlevel 1 (echo 没有 python 进程：两个后台都没起来) else (echo 有 python 进程在跑)
echo.
echo ==== 6. 最近日志 ====
if exist reply.log (
    powershell -NoProfile -Command "Get-Content reply.log -Tail 10 -Encoding utf8"
) else (
    echo 还没有 reply.log
)
echo.
pause