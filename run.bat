@echo off
rem ===========================================================================
rem  睡眠监测 (SleepMonitor) — Windows 一键启动
rem
rem  双击即可: 自动建虚拟环境、装依赖、启动程序并打开浏览器。
rem  也可以带参数, 会原样传给程序, 例如:
rem      run.bat list-ports
rem      run.bat run --pressure-port COM5 --vitals-port COM4
rem      run.bat selftest
rem ===========================================================================
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "VENV_DIR=%~dp0.venv"
set "PY_EXE=%VENV_DIR%\Scripts\python.exe"
set "STAMP=%VENV_DIR%\.requirements.stamp"

rem ---------- 1. 找一个可用的 Python ----------
set "LAUNCHER="
py -3 --version >nul 2>&1 && set "LAUNCHER=py -3"
if not defined LAUNCHER (
    python --version >nul 2>&1 && set "LAUNCHER=python"
)
if not defined LAUNCHER (
    echo.
    echo [错误] 没有找到 Python。
    echo.
    echo 请先安装 Python 3.10 或更高版本: https://www.python.org/downloads/windows/
    echo 安装时务必勾选 "Add Python to PATH"。
    echo.
    pause
    exit /b 1
)
echo [1/4] 使用 Python: %LAUNCHER%
%LAUNCHER% --version

rem ---------- 2. 建虚拟环境 ----------
if not exist "%PY_EXE%" (
    echo [2/4] 首次运行, 正在创建虚拟环境 .venv ...
    %LAUNCHER% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败。
        pause
        exit /b 1
    )
) else (
    echo [2/4] 虚拟环境已存在, 跳过创建。
)

rem ---------- 3. 装依赖 (requirements.txt 变了才重装) ----------
set "NEED_INSTALL=1"
if exist "%STAMP%" (
    for /f "delims=" %%A in ('certutil -hashfile "%~dp0requirements.txt" MD5 ^| find /v ":"') do set "REQ_HASH=%%A"
    set /p OLD_HASH=<"%STAMP%"
    if "!REQ_HASH!"=="!OLD_HASH!" set "NEED_INSTALL=0"
)

if "%NEED_INSTALL%"=="1" (
    echo [3/4] 正在安装依赖, 首次会慢一些 ...
    "%PY_EXE%" -m pip install --upgrade pip --quiet --disable-pip-version-check
    "%PY_EXE%" -m pip install -r "%~dp0requirements.txt" --disable-pip-version-check
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败。若是网络问题, 可改用国内镜像后重试:
        echo     "%PY_EXE%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        echo.
        pause
        exit /b 1
    )
    for /f "delims=" %%A in ('certutil -hashfile "%~dp0requirements.txt" MD5 ^| find /v ":"') do set "REQ_HASH=%%A"
    >"%STAMP%" echo !REQ_HASH!
) else (
    echo [3/4] 依赖已是最新, 跳过安装。
)

rem ---------- 4. 启动 ----------
echo [4/4] 启动中 ...
echo.
"%PY_EXE%" -m sleepmonitor %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo 程序退出, 返回码 %EXIT_CODE%。
    pause
)
endlocal & exit /b %EXIT_CODE%
