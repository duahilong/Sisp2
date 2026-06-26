import subprocess
from typing import Any


POWERSHELL_BASE_ARGS = [
    "powershell",
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-Command",
]

POWERSHELL_DEFAULT_TIMEOUT = 600



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



def run_powershell(script: str, timeout: float = POWERSHELL_DEFAULT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*POWERSHELL_BASE_ARGS, "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8; " + script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )
