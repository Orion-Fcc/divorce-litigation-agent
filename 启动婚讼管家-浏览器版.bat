@echo off
chcp 65001 >nul
title 婚讼管家 · 浏览器版
cd /d "%~dp0python_app"

python --version >nul 2>nul
if not errorlevel 1 (
    python main.py --auto-init --open-browser
    if errorlevel 1 (
        echo.
        echo  [错误] 应用启动失败，请查看上方日志。常见原因：网络不通、pip 安装失败。
        pause
    )
    goto :end
)

py -3 --version >nul 2>nul
if not errorlevel 1 (
    py -3 main.py --auto-init --open-browser
    if errorlevel 1 (
        echo.
        echo  [错误] 应用启动失败，请查看上方日志。常见原因：网络不通、pip 安装失败。
        pause
    )
    goto :end
)

echo.
echo  [错误] 未找到 Python。请先安装 Python 3.9+（安装时勾选 Add to PATH），
echo         然后重新双击本脚本。下载地址：https://www.python.org/downloads/
echo.
pause

:end
