# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（构建事实来源）。

build.bat 与 CI 都直接使用本文件，新增模块时只需在这里补充 hiddenimports。
"""

hiddenimports = [
    "app.application",
    "app.application.contracts",
    "app.application.planning",
    "app.application.reporting",
    "app.application.service",
    "app.cli",
    "app.disk_safety",
    "app.drive_letters",
    "app.logging_setup",
    "app.models",
    "app.orchestrator",
    "app.paths",
    "app.results",
    "app.version",
    "app.worker_launcher",
    "app.modules.common",
    "app.modules.common.service",
    "app.modules.common.constants",
    "app.modules.config_loader",
    "app.modules.config_loader.service",
    "app.modules.disk_info",
    "app.modules.disk_info.service",
    "app.modules.disk_initializer",
    "app.modules.disk_initializer.service",
    "app.modules.disk_partitioner",
    "app.modules.disk_partitioner.service",
    "app.modules.ghost_writer",
    "app.modules.ghost_writer.service",
    "app.modules.directory_copier",
    "app.modules.directory_copier.service",
    "app.modules.boot_creator",
    "app.modules.boot_creator.service",
    "app.modules.initialization_validator",
    "app.modules.initialization_validator.service",
    "app.modules.partition_validator",
    "app.modules.partition_validator.service",
    "app.modules.user_interaction",
    "app.modules.user_interaction.service",
    "app.preflight",
]


a = Analysis(
    ['app\\main.py'],
    pathex=[],
    binaries=[],
    datas=[('json', 'json')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Sisp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Sisp',
)
