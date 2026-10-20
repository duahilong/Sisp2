@echo off
chcp 65001 >nul
echo ========================================
echo Sisp build script
echo ========================================

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

echo Cleaning previous build files...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build

if not exist Sisp.spec (
    echo Build failed: Sisp.spec was not found.
    pause
    exit /b 1
)

echo Building with %PYTHON_CMD% using Sisp.spec...
"%PYTHON_CMD%" -m PyInstaller --clean --noconfirm Sisp.spec

if %errorlevel% neq 0 (
    echo Build failed!
    pause
    exit /b 1
)

echo Build completed!
echo Output file: dist\Sisp\Sisp.exe

echo.
echo Creating release directories...
if not exist dist\Sisp\json mkdir dist\Sisp\json
if not exist dist\Sisp\Sw mkdir dist\Sisp\Sw

echo Copying config file...
copy json\win11.json dist\Sisp\json\

echo Copying external tools...
if exist Sw\ghost64.exe (
    copy Sw\ghost64.exe dist\Sisp\Sw\
) else (
    echo Warning: Sw\ghost64.exe was not found. Add it before release.
)

if exist Sw\bcdboot.exe (
    copy Sw\bcdboot.exe dist\Sisp\Sw\
) else (
    echo Warning: Sw\bcdboot.exe was not found. Add it before release.
)

echo.
echo ========================================
echo Build completed!
echo Release directory: dist\Sisp
echo Confirm these files exist:
echo   - dist\Sisp\Sisp.exe
echo   - dist\Sisp\_internal
echo   - dist\Sisp\json\win11.json
echo   - dist\Sisp\Sw\ghost64.exe
echo   - dist\Sisp\Sw\bcdboot.exe
echo ========================================
pause
