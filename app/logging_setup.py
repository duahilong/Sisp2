"""统一日志配置。

默认写入 ``%LOCALAPPDATA%\\Sisp\\logs``（无法创建时回退到临时目录）。
日志文件按运行轮转，方便在多盘 worker 场景下按时间/硬盘排查问题。
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LOGGER_NAME = "sisp"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
MAX_LOG_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5
MAX_SCRIPT_CHARS = 8000
MAX_OUTPUT_CHARS = 4000

_log_path: Path | None = None


def default_log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "Sisp" / "logs"


def configure_logging(
    log_dir: str | Path | None = None,
    run_id: str | None = None,
    level: int = logging.DEBUG,
) -> Path | None:
    """配置文件日志并返回日志路径；无法创建文件时返回 None。"""
    global _log_path

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, "_sisp_handler", False):
            logger.removeHandler(handler)
            handler.close()

    directory = Path(log_dir) if log_dir else default_log_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        directory = Path(tempfile.gettempdir()) / "Sisp" / "logs"
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            _log_path = None
            return None

    # 加入进程号，避免多盘 worker 在同一秒启动时写入同一个文件。
    filename = f"sisp_{run_id or time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.log"
    path = directory / filename
    try:
        handler = RotatingFileHandler(
            path,
            maxBytes=MAX_LOG_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError:
        _log_path = None
        return None

    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler._sisp_handler = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    _log_path = path
    logger.info("日志已初始化: %s", path)
    return path


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def get_log_path() -> Path | None:
    return _log_path


def reset_logging() -> None:
    """测试辅助：移除本模块添加的所有 handler。"""
    global _log_path
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if getattr(handler, "_sisp_handler", False):
            logger.removeHandler(handler)
            handler.close()
    _log_path = None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...（已截断，共 {len(text)} 字符）"


def log_powershell(script: str, completed: Any, duration_seconds: float) -> None:
    """记录 PowerShell 脚本、退出码和输出，供失败排查使用。"""
    logger = get_logger("powershell")
    logger.debug("PowerShell 脚本:\n%s", _truncate(script, MAX_SCRIPT_CHARS))

    returncode = getattr(completed, "returncode", None)
    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    if returncode == 0:
        logger.info("PowerShell 完成: returncode=0, 耗时=%.3fs", duration_seconds)
    else:
        logger.warning(
            "PowerShell 失败: returncode=%s, 耗时=%.3fs\nstderr:\n%s",
            returncode,
            duration_seconds,
            _truncate(stderr, MAX_OUTPUT_CHARS),
        )
    if stdout:
        logger.debug("PowerShell stdout:\n%s", _truncate(stdout, MAX_OUTPUT_CHARS))
    if stderr and returncode == 0:
        logger.debug("PowerShell stderr:\n%s", _truncate(stderr, MAX_OUTPUT_CHARS))


def log_step(disk_number: int | None, name: str, passed: bool, message: str, duration_seconds: float = 0.0) -> None:
    logger = get_logger("step")
    status = "通过" if passed else "失败"
    logger.info(
        "硬盘 %s %s%s: %s（耗时 %.3fs）",
        disk_number,
        name,
        status,
        message,
        duration_seconds,
    )


def log_exception(disk_number: int | None, exc: BaseException) -> None:
    get_logger("error").exception("硬盘 %s 执行失败: %s", disk_number, exc)
