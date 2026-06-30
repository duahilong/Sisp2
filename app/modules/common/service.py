import os
import subprocess
from typing import Any

from app.modules.common.constants import ErrorCode


POWERSHELL_BASE_ARGS = [
    "powershell",
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-Command",
]

POWERSHELL_DEFAULT_TIMEOUT = 600


class SispError(Exception):
    """Sisp 项目自定义异常基类"""
    
    def __init__(self, message: str, error_code: int = ErrorCode.SUCCESS):
        super().__init__(message)
        self.error_code = error_code


class PreflightError(SispError):
    """运行前检查失败异常"""
    
    def __init__(self, message: str):
        super().__init__(message, ErrorCode.PREFLIGHT_FAILED)


class InitializationError(SispError):
    """硬盘初始化失败异常"""
    
    def __init__(self, message: str):
        super().__init__(message, ErrorCode.INITIALIZATION_FAILED)


class PartitionError(SispError):
    """分区和格式化失败异常"""
    
    def __init__(self, message: str):
        super().__init__(message, ErrorCode.PARTITION_FAILED)


class GhostError(SispError):
    """Ghost 镜像写入失败异常"""
    
    def __init__(self, message: str):
        super().__init__(message, ErrorCode.GHOST_FAILED)


class CopyError(SispError):
    """目录拷贝失败异常"""
    
    def __init__(self, message: str):
        super().__init__(message, ErrorCode.COPY_FAILED)


class BootError(SispError):
    """引导记录创建失败异常"""
    
    def __init__(self, message: str):
        super().__init__(message, ErrorCode.BOOT_FAILED)


class ValidationError(SispError):
    """验证失败异常"""
    
    def __init__(self, message: str):
        super().__init__(message, ErrorCode.VALIDATION_FAILED)


class ConfigError(SispError):
    """配置错误异常"""
    
    def __init__(self, message: str):
        super().__init__(message, ErrorCode.CONFIG_ERROR)


class DiskProtectedError(SispError):
    """硬盘受保护异常"""
    
    def __init__(self, message: str):
        super().__init__(message, ErrorCode.DISK_PROTECTED)


class IdentityMismatchError(SispError):
    """硬盘身份不匹配异常"""
    
    def __init__(self, message: str):
        super().__init__(message, ErrorCode.IDENTITY_MISMATCH)


class PathTraversalError(SispError):
    """路径遍历攻击异常"""
    
    def __init__(self, message: str):
        super().__init__(message, ErrorCode.PATH_TRAVERSAL)



def validate_integer_param(name: str, value: Any) -> int:
    """验证参数为整数，防止 PowerShell 脚本注入"""
    if not isinstance(value, int):
        raise ValueError(f"{name} 必须为整数，实际为: {type(value).__name__}")
    return value



def validate_string_param(name: str, value: Any, allow_empty: bool = False) -> str:
    """验证参数为字符串，防止 PowerShell 脚本注入"""
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须为字符串，实际为: {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} 不能为空")
    return value



def get_system_drive_letter() -> str:
    return os.environ.get("SystemDrive", "C:")[0].upper()



def run_powershell(script: str, timeout: float = POWERSHELL_DEFAULT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [*POWERSHELL_BASE_ARGS, "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8; " + script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=f"PowerShell 执行超时（{timeout} 秒）",
        )
