@echo off
title 婚讼管家 - 浏览器版
cd /d "%~dp0python_app"

set "PY="
python --version >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
    py -3 --version >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    for /d %%i in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if exist "%%i\python.exe" (
            set "PY=%%i\python.exe"
            goto :py_found
        )
    )
)
:py_found

if defined PY goto :run
echo.
echo  [错误] 未找到 Python。请先安装 Python 3.9+（安装时勾选 Add to PATH），
echo         然后重新双击本脚本。下载地址：https://www.python.org/downloads/
echo.
pause
goto :end

:run
if "%PY%"=="py -3" (
    py -3 main.py --auto-init --open-browser
) else (
    "%PY%" main.py --auto-init --open-browser
)
if errorlevel 1 (
    echo.
    echo  [错误] 应用启动失败，请查看上方日志。常见原因：网络不通或 pip 安装失败。
    pause
)

:end
