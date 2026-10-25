"""GUI 应用启动与 UAC 提权。"""

from __future__ import annotations

import os
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from app.gui.elevation import is_admin, relaunch_as_admin
from app.gui.main_window import MainWindow
from app.logging_setup import configure_logging, get_logger, reset_logging
from app.version import __version__


def _log_dir_from_arguments(arguments: list[str]) -> str | None:
    """解析 ``--log-dir <路径>`` / ``--log-dir=<路径>``；未指定时返回 None。

    未指定时沿用 :func:`app.logging_setup.default_log_dir` 的默认目录。
    """
    for index, token in enumerate(arguments):
        if token == "--log-dir":
            if index + 1 < len(arguments):
                value = arguments[index + 1].strip()
                return value or None
            return None
        if token.startswith("--log-dir="):
            value = token.split("=", 1)[1].strip()
            return value or None
    return None


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

    # GUI 与 CLI 共用文件日志：结果页的“日志文件”和“打开选中日志”依赖它。
    log_path = configure_logging(_log_dir_from_arguments(arguments))
    logger = get_logger("gui")
    logger.info("GUI 启动: version=%s, 日志=%s", __version__, log_path or "未启用")
    try:
        app = QApplication(arguments)
        app.setApplicationName("Sisp")
        app.setApplicationDisplayName("Sisp - 系统安装工具")
        app.setApplicationVersion(__version__)

        window = MainWindow()
        window.show()
        return app.exec()
    finally:
        logger.info("GUI 退出")
        reset_logging()
