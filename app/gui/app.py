"""GUI 应用启动与 UAC 提权。"""

from __future__ import annotations

import os
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from app.gui.elevation import is_admin, relaunch_as_admin
from app.gui.main_window import MainWindow
from app.version import __version__


def main(argv: list[str] | None = None) -> int:
    """启动图形界面；非管理员时先尝试通过 UAC 重新启动。"""
    arguments = list(sys.argv if argv is None else argv)
    allow_unprivileged = os.environ.get("SISP_NO_ELEVATE") == "1"

    if not is_admin() and not allow_unprivileged:
        if relaunch_as_admin(arguments[1:]):
            return 0
        app = QApplication(arguments)
        QMessageBox.critical(
            None,
            "需要管理员权限",
            "Sisp 需要以管理员身份运行。\n\n"
            "UAC 提权被取消或失败，请右键程序并选择“以管理员身份运行”。",
        )
        app.quit()
        return 1

    app = QApplication(arguments)
    app.setApplicationName("Sisp")
    app.setApplicationDisplayName("Sisp - 系统安装工具")
    app.setApplicationVersion(__version__)

    window = MainWindow()
    window.show()
    return app.exec()
