@echo off
title 微信小号代理服务（单独启动）
cd /d "%~dp0.."
set "WXBOT_HOME=%~dp0.."
call "%WXBOT_HOME%\tools\python_path.bat"
set "PYTHONPATH=%~dp0..;%~dp0..\agent;%~dp0..\memory;%~dp0;%~dp0..\pylibs"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
"%PY%" "%~dp0wx_service.py" %*
pause