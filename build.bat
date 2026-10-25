@echo off
setlocal
chcp 65001 >nul
echo ========================================
echo Sisp build script
echo ========================================
echo Usage:
echo   build.bat          Build CLI (Sisp.exe)
echo   build.bat --gui    Build GUI (SispGUI.exe)
echo   build.bat --all    Build both
echo ========================================

set "BUILD_CLI=1"
set "BUILD_GUI=0"

if /i "%~1"=="--gui" (
    set "BUILD_CLI=0"
    set "BUILD_GUI=1"
)
if /i "%~1"=="--all" (
    set "BUILD_CLI=1"
    set "BUILD_GUI=1"
)

set "PYTHON_CMD="

if not "%SISP_PYTHON%"=="" (
    set "PYTHON_CMD=%SISP_PYTHON%"
) else (
    where py >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=py"
)

if "%PYTHON_CMD%"=="" (
    where python >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if "%PYTHON_CMD%"=="" (
    echo Build failed: no Python command was found.
    echo Install Python, or set SISP_PYTHON to the full path of python.exe, then run again.
    pause
    exit /b 1
)

"%PYTHON_CMD%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo Build failed: PyInstaller is not available for %PYTHON_CMD%.
    echo Run: "%PYTHON_CMD%" -m pip install pyinstaller
    pause
    exit /b 1
)

if "%BUILD_GUI%"=="1" (
    "%PYTHON_CMD%" -c "import PySide6" >nul 2>&1
    if errorlevel 1 (
        echo Build failed: PySide6 is not available for %PYTHON_CMD%.
        echo Run: "%PYTHON_CMD%" -m pip install -r requirements-gui.txt
        pause
        exit /b 1
    )
)

echo Cleaning previous build files...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build

if "%BUILD_CLI%"=="1" (
    call :build_one Sisp.spec Sisp
    if errorlevel 1 exit /b 1
)

if "%BUILD_GUI%"=="1" (
    call :build_one SispGUI.spec SispGUI
    if errorlevel 1 exit /b 1
)

echo.
echo ========================================
echo Build completed!
if "%BUILD_CLI%"=="1" (
    echo CLI: dist\Sisp\Sisp.exe
)
if "%BUILD_GUI%"=="1" (
    echo GUI: dist\SispGUI\SispGUI.exe
)
echo Confirm these files exist:
if "%BUILD_CLI%"=="1" (
    echo   - dist\Sisp\Sisp.exe
    echo   - dist\Sisp\_internal
    echo   - dist\Sisp\json\win11.json
    echo   - dist\Sisp\Sw\ghost64.exe
    echo   - dist\Sisp\Sw\bcdboot.exe
)
if "%BUILD_GUI%"=="1" (
    echo   - dist\SispGUI\SispGUI.exe
    echo   - dist\SispGUI\_internal
    echo   - dist\SispGUI\json\win11.json
    echo   - dist\SispGUI\Sw\ghost64.exe
    echo   - dist\SispGUI\Sw\bcdboot.exe
)
echo ========================================
pause
exit /b 0

:build_one
if not exist "%~1" (
    echo Build failed: %~1 was not found.
    pause
    exit /b 1
)

echo.
echo Building with %PYTHON_CMD% using %~1...
"%PYTHON_CMD%" -m PyInstaller --clean --noconfirm "%~1"
if errorlevel 1 (
    echo Build failed: %~1
    pause
    exit /b 1
)

set "OUT_DIR=dist\%~2"

echo Creating release directories for %OUT_DIR%...
if not exist "%OUT_DIR%\json" mkdir "%OUT_DIR%\json"
if not exist "%OUT_DIR%\Sw" mkdir "%OUT_DIR%\Sw"

echo Copying config file...
copy /y json\win11.json "%OUT_DIR%\json\" >nul

echo Copying external tools...
if exist Sw\ghost64.exe (
    copy /y Sw\ghost64.exe "%OUT_DIR%\Sw\" >nul
) else (
    echo Warning: Sw\ghost64.exe was not found. Add it before release.
)

if exist Sw\bcdboot.exe (
    copy /y Sw\bcdboot.exe "%OUT_DIR%\Sw\" >nul
) else (
    echo Warning: Sw\bcdboot.exe was not found. Add it before release.
)

exit /b 0
