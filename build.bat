@echo off
chcp 65001 >nul
echo ========================================
echo Sisp 打包脚本
echo ========================================

echo 清理旧的构建文件...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
if exist Sisp.spec del Sisp.spec

echo 开始打包...
pyinstaller --onefile --name Sisp --console ^
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
    echo 打包失败！
    pause
    exit /b 1
)

echo 打包完成！
echo 输出文件: dist\Sisp.exe

echo.
echo 创建发布目录...
if not exist dist\json mkdir dist\json
if not exist dist\Sw mkdir dist\Sw

echo 复制配置文件...
copy json\win11.json dist\json\

echo.
echo ========================================
echo 打包完成！
echo 请将以下文件复制到发布目录：
echo   - dist\Sisp.exe
echo   - dist\json\win11.json
echo   - Sw\ghost64.exe (需要手动复制)
echo   - Sw\bcdboot.exe (需要手动复制)
echo ========================================
pause
