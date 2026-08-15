@echo off
chcp 65001 >nul
title Future Scene - Demo Launcher
cd /d "%~dp0"

echo ============================================================
echo   Future Scene · 智能虚拟角色自主直播系统 - 本地演示启动
echo ============================================================
echo.

echo [1/4] 检查 Python 环境...
where python >nul 2>nul
if errorlevel 1 (
    echo   未找到 Python，请先安装 Python 3.10 及以上版本（勾选 Add to PATH）。
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python -c "import sys;print(sys.version.split()[0])" 2^>nul') do set PYVER=%%v
echo   检测到 Python %PYVER%

echo [2/4] 检查项目依赖...
python -c "import flask, openai, zhipuai, websockets, requests, mss, PIL" >nul 2>nul
if errorlevel 1 (
    echo   依赖缺失，正在安装 requirements.txt...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo   依赖安装失败，请检查网络后重试。
        pause
        exit /b 1
    )
)
echo   依赖就绪

echo [3/4] 检查 .env 配置...
if not exist .env (
    copy .env.example .env >nul
    echo   已生成 .env 模板。
    echo   >>> 请打开 .env 填写 API Key（OPENAI_API_KEY / ZHIPU_API_KEY / DASHSCOPE_API_KEY 等），
    echo   >>> 保存后重新双击本脚本启动。缺失 Key 时服务将无法通过环境校验。
    pause
    exit /b 1
)
echo   .env 已存在

echo [4/4] 启动服务，浏览器将自动打开控制台...
echo   停止服务：在窗口按 Ctrl+C
echo.
python run.py

echo.
echo 服务已退出。
pause
