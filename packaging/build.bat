@echo off
REM ===========================================================================
REM  美股期权策略推荐器 - 一键构建 Windows 安装包
REM ---------------------------------------------------------------------------
REM  前置条件：
REM    * Python 3.9+ 且已 pip install pyinstaller
REM    * Inno Setup 6.3+（提供 ISCC.exe 命令行编译器）
REM  产物：
REM    dist\USOptionsAdvisor-Setup-<版本>-win64.exe
REM ===========================================================================
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0.."

echo.
echo ============================================================
echo   美股期权策略推荐器 - 构建安装包
echo ============================================================
echo.

REM ---- 1/3 生成应用图标 ----
echo [1/3] 生成应用图标 app.ico ...
python packaging\make_icon.py packaging\app.ico
if errorlevel 1 goto :err

REM ---- 2/3 PyInstaller 打包 ----
echo.
echo [2/3] PyInstaller 打包（onedir）...
python -m PyInstaller --clean --noconfirm --distpath dist --workpath build packaging\us-options-advisor.spec
if errorlevel 1 goto :err

REM ---- 3/3 Inno Setup 编译安装包 ----
echo.
echo [3/3] 编译安装包 ...
set "ISCC="
for %%P in (
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
    "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
) do (
    if not defined ISCC if exist %%P set "ISCC=%%~P"
)
if not defined ISCC (
    echo [错误] 未找到 ISCC.exe，请先安装 Inno Setup 6：
    echo        https://jrsoftware.org/isdl.php
    goto :err
)
echo       使用编译器: !ISCC!
"!ISCC!" packaging\us-options-advisor.iss
if errorlevel 1 goto :err

echo.
echo ============================================================
echo   构建完成！
echo   安装包位于 dist\ 目录。
echo ============================================================
echo.
goto :eof

:err
echo.
echo *** 构建失败，请检查上面的错误信息 ***
exit /b 1
