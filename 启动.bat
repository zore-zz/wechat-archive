@echo off
setlocal
chcp 936 >nul 2>nul
cd /d "%~dp0"
title 公众号大事记助手
set "PYEXE="
set "PYARGS="

rem ========== 1. 常见安装位置 ==========
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :probe "%%~D\python.exe"
for /d %%D in ("C:\Python3*") do call :probe "%%~D\python.exe"
if defined PYEXE goto run

rem ========== 2. PATH 里的 python（跳过应用商店占位符）==========
for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PYEXE (
        set "CAND=%%~P"
        call :probe_not_store "%%~P"
    )
)
if defined PYEXE goto run

rem ========== 3. py 启动器 ==========
for /f "delims=" %%P in ('where py 2^>nul') do (
    if not defined PYEXE call :probe_args "%%~P" "-3"
)
if defined PYEXE goto run

echo.
echo  ==================================================
echo    没有找到可用的 Python
echo  ==================================================
echo.
echo    这个脚本需要 Python 3 和几个组件才能跑。
echo    解决办法（任选一个）：
echo.
echo    1. 打开 https://www.python.org/downloads/ 下载安装
echo       安装第一屏务必勾选 "Add python.exe to PATH"
echo    2. 装完重新双击本文件即可，不用改任何东西
echo.
echo    如果本机之前能用，可能是安全软件删了 Python，
echo    重装一次就行，数据库和已有成果都在，不会丢。
echo.
pause
exit /b 1

:run
echo.
"%PYEXE%" %PYARGS% run.py menu
set "RC=%errorlevel%"
echo.
echo  --------------------------------------------------
echo    程序已退出（代码 %RC%）
echo    上面若有报错，截图发我即可，数据都保存着
pause
exit /b %RC%

:probe
rem 用法：call :probe "解释器完整路径"
if defined PYEXE goto :eof
if not exist "%~1" goto :eof
"%~1" -c "import yaml,openpyxl" >nul 2>nul
if not errorlevel 1 set "PYEXE=%~1"
goto :eof

:probe_not_store
rem 用法：call :probe_not_store "路径"，命中 WindowsApps 就跳过
if defined PYEXE goto :eof
echo %~1 | findstr /i "WindowsApps" >nul && goto :eof
call :probe "%~1"
goto :eof

:probe_args
rem 用法：call :probe_args "解释器路径" "参数"
if defined PYEXE goto :eof
if not exist "%~1" goto :eof
"%~1" %~2 -c "import yaml,openpyxl" >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=%~1"
    set "PYARGS=%~2"
)
goto :eof
