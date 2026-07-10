@echo off
chcp 65001 >nul 2>&1
title Cloudflare Auto Task
cd /d "%~dp0"
cls

echo ============================================
echo    Cloudflare Auto Task
echo    正在启动，请稍候...
echo ============================================
echo.

:: ---------- 检查 Python ----------
set PYTHON=
where python >nul 2>&1
if %errorlevel%==0 (
    set PYTHON=python
) else (
    where py >nul 2>&1
    if %errorlevel%==0 (
        set PYTHON=py -3
    )
)

if "%PYTHON%"=="" (
    echo [错误] 未检测到 Python，请先安装：
    echo   访问 https://www.python.org/downloads/ 下载安装
    echo   安装时务必勾选 "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('%PYTHON% --version 2^>^&1') do echo [OK] %%i

:: ---------- 检查 Google Chrome ----------
set CHROME_FOUND=0
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" set CHROME_FOUND=1
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set CHROME_FOUND=1
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set CHROME_FOUND=1

if %CHROME_FOUND%==0 (
    echo.
    echo [警告] 未在默认路径检测到 Google Chrome
    echo   如已安装请忽略此提示，否则请安装: https://www.google.com/chrome/
    echo.
)
if %CHROME_FOUND%==1 echo [OK] Google Chrome 已安装

:: ---------- 创建虚拟环境 ----------
if not exist ".venv\Scripts\activate.bat" (
    echo.
    echo [首次运行] 正在创建虚拟环境...
    %PYTHON% -m venv .venv
    if %errorlevel% neq 0 (
        echo [错误] 创建虚拟环境失败
        pause
        exit /b 1
    )
    echo [OK] 虚拟环境创建成功
)

call .venv\Scripts\activate.bat

:: ---------- 安装依赖 ----------
python -c "import flask" >nul 2>&1
if %errorlevel% neq 0 (
    echo [安装] 正在安装依赖包（仅首次需要，请耐心等待）...
    python -m pip install --upgrade pip >nul 2>&1
    python -m pip install flask pyyaml requests selenium selenium-stealth waitress webdriver-manager faker
    if %errorlevel% neq 0 (
        echo.
        echo [错误] 依赖安装失败，请检查网络连接
        pause
        exit /b 1
    )
    echo [OK] 依赖安装完成
)

:: ---------- 创建默认配置文件 ----------
if not exist "config.yaml" (
    if exist "config.example.yaml" (
        echo [配置] 正在生成默认配置文件...
        copy /y config.example.yaml config.yaml >nul
        echo [OK] 已生成 config.yaml
    )
)

:: ---------- 启动服务 ----------
echo.
echo ============================================
echo    启动成功！浏览器将自动打开控制台
echo.
echo    如未自动打开，请手动访问:
echo    http://localhost:5000
echo.
echo    关闭此窗口即可停止程序
echo ============================================
echo.

:: 延迟打开浏览器
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:5000"

:: 启动 Web 服务
python server.py

:: 如果服务异常退出
echo.
echo [提示] 程序已停止
pause
