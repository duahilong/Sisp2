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
if exist Sisp.spec del Sisp.spec

echo Building with %PYTHON_CMD%...
"%PYTHON_CMD%" -m PyInstaller --onedir --name Sisp --console ^
  --icon=app.ico ^
  --add-data "json;json" ^
  --hidden-import app.modules.common ^
  --hidden-import app.modules.common.service ^
  --hidden-import app.modules.common.constants ^
  --hidden-import app.modules.config_loader ^
  --hidden-import app.modules.config_loader.service ^
  --hidden-import app.modules.disk_info ^
  --hidden-import app.modules.disk_info.service ^
  --hidden-import app.modules.disk_initializer ^
  --hidden-import app.modules.disk_initializer.service ^
  --hidden-import app.modules.disk_partitioner ^
  --hidden-import app.modules.disk_partitioner.service ^
  --hidden-import app.modules.ghost_writer ^
  --hidden-import app.modules.ghost_writer.service ^
  --hidden-import app.modules.directory_copier ^
  --hidden-import app.modules.directory_copier.service ^
  --hidden-import app.modules.boot_creator ^
  --hidden-import app.modules.boot_creator.service ^
  --hidden-import app.modules.initialization_validator ^
  --hidden-import app.modules.initialization_validator.service ^
  --hidden-import app.modules.partition_validator ^
  --hidden-import app.modules.partition_validator.service ^
  --hidden-import app.modules.user_interaction ^
  --hidden-import app.modules.user_interaction.service ^
  --hidden-import app.preflight ^
  app\main.py

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
