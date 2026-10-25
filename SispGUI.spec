# -*- mode: python ; coding: utf-8 -*-
"""PySide6 图形界面打包配置。

与 ``Sisp.spec`` 并列：``Sisp.exe`` 保持命令行控制台模式，``SispGUI.exe``
为窗口模式并在清单中声明管理员权限（``uac_admin=True``），双击即可触发
UAC 提权。build.bat 与 CI 都直接使用本文件。
"""

hiddenimports = [
    "app.application",
    "app.application.cancellation",
    "app.application.contracts",
    "app.application.events",
    "app.application.jobs",
    "app.application.planning",
    "app.application.reporting",
    "app.application.service",
    "app.application.workflow",
    "app.cli",
    "app.disk_safety",
    "app.drive_letters",
    "app.gui",
    "app.gui.app",
    "app.gui.elevation",
    "app.gui.jobs_bridge",
    "app.gui.main_window",
    "app.gui_main",
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
    ['app\\gui_main.py'],
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
    name='SispGUI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app.ico'],
    uac_admin=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SispGUI',
)
