"""写盘前的纯校验逻辑。

从 ``app.orchestrator`` 抽出的可复用部分：不打印、不启动 worker、不执行
任何磁盘操作，只负责路径和资源位置校验。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.disk_safety import (
    build_resource_paths,
    resolve_path_disk_number,
    validate_resources_not_on_target_disks,
)
from app.modules.common.service import ConfigError

PathDiskResolver = Callable[[str | Path], int | None]


def validate_required_paths(config_payload: dict[str, Any]) -> None:
    """校验 Ghost、镜像、目录拷贝源和 bcdboot 路径。

    对文件/目录类型也做检查，避免把目录误当成可执行文件，或把文件误当成
    目录拷贝源。该函数会在任何写盘动作前以及 Ghost 写入前各调用一次。
    """
    required_paths: dict[str, tuple[Any, str]] = {
        "Ghost 可执行文件路径 (gho_exe)": (
            (config_payload.get("software_paths") or {}).get("ghost64_path"),
            "file",
        ),
        "Ghost 镜像文件路径 (win_gho)": (
            (config_payload.get("image_info") or {}).get("image_path"),
            "file",
        ),
        "目录拷贝源路径 (software_file)": (
            (config_payload.get("copy_info") or {}).get("source_dir"),
            "dir",
        ),
        "bcdboot 可执行文件路径 (bcd_exe)": (
            (config_payload.get("software_paths") or {}).get("bcdboot_path"),
            "file",
        ),
    }
    for name, (value, expected_kind) in required_paths.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"配置中缺少 {name}")
        path = Path(value)
        if not path.exists():
            raise ConfigError(f"{name} 不存在: {value}")
        if expected_kind == "file" and not path.is_file():
            raise ConfigError(f"{name} 不是文件: {value}")
        if expected_kind == "dir" and not path.is_dir():
            raise ConfigError(f"{name} 不是目录: {value}")


def validate_target_resources(
    config_payload: dict[str, Any],
    target_disk_numbers: list[int],
    config_path: str | Path | None = None,
    log_dir: str | Path | None = None,
    result_dir: str | Path | None = None,
    resolver: PathDiskResolver | None = None,
) -> None:
    """拒绝把 GHO、工具、源目录、配置或日志放在目标盘上。"""
    resources = build_resource_paths(
        config_payload,
        config_path=config_path,
        log_dir=log_dir,
        result_dir=result_dir,
    )
    validate_resources_not_on_target_disks(
        resources,
        target_disk_numbers,
        resolver=resolver or resolve_path_disk_number,
    )
