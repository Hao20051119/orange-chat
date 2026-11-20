@echo off
title 微信小号 - 停止
cd /d "%~dp0"
echo 正在停止本项目的后台进程...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\stop.ps1"
echo.
ping -n 3 127.0.0.1 >nul