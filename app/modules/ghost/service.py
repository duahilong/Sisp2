"""Ghost 还原命令的纯逻辑：拼命令行、认镜像头、读 ``ghosterr.txt``。

**为什么不把它塞进 phases 里**：这三件事都不碰磁盘、也不 spawn 进程，
放在模块层可以被计划期校验（``workflow._image_problems``）、界面探测
（``/api/images``）和执行期阶段（``phases/ghost.py``）三处复用，各写一份
迟早会漂移。

Ghost 11.x 的三条硬规则（来自实测与原型项目）：

1. ``dst`` 用 Ghost 自己的编号——**磁盘从 1 开始、分区从 1 开始**；
2. ``src=<文件>:N`` 的 ``N`` 是镜像内部的**分区块序号**，不是目标分区号；
3. 失败原因写在**当前工作目录**的 ``ghosterr.txt``；退出码非 0 只能说明"失败了"，
   说不清为什么，所以执行时要把 ``cwd`` 指到日志目录再回头读这个文件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# 装机只认 Ghost 分区块镜像；``.ghs`` 是 Ghost 的"分卷/增量"后缀，先一并接受。
GHO_SUFFIXES: tuple[str, ...] = (".gho", ".ghs")

# 所有 Ghost 镜像的文件头都是这两个字节（实测 ``test.GHO`` / ``Intel_WIN11.GHO``）。
GHO_MAGIC = b"\xfe\xef"

# 0 = 让 Ghost 用它自己的默认值（分区块 1）
DEFAULT_BLOCK_INDEX = 1


def ghost_command(
    ghost_exe: str,
    gho_path: str,
    *,
    ghost_disk: int,
    partition: int,
    block_index: int = DEFAULT_BLOCK_INDEX,
) -> list[str]:
    """拼 Ghost 还原命令（纯拼装，不执行）。

    - ``-sure``：不再弹"确定要覆盖分区吗"；
    - ``-batch``：跑完自动退出（否则进程停在结尾等按键，父进程会一直等到超时）；
    - ``-ntexact``：NTFS 按原样还原（原型一直在用；不加的话 Ghost 可能自行调整簇大小）；
    - **不加** ``-rb``：写镜像之后还要跑 bcdboot，重启了就没机会了。
    """
    if not str(ghost_exe or "").strip():
        raise ValueError("ghost64.exe 路径为空，无法写入 GHO 镜像")
    if not str(gho_path or "").strip():
        raise ValueError("GHO 镜像路径为空")
    disk = int(ghost_disk)
    part = int(partition)
    if disk < 1 or part < 1:
        raise ValueError(f"Ghost 目标是 1 基编号，收到 dst={disk}:{part}")
    index = int(block_index)
    if index < 1:
        raise ValueError(f"分区块序号必须 ≥ 1，收到 {block_index}")
    return [
        str(ghost_exe),
        f"-clone,mode=pload,src={gho_path}:{index},dst={disk}:{part}",
        "-sure",
        "-batch",
        "-ntexact",
    ]


def sniff_gho(path: str | Path) -> dict[str, Any]:
    """只看文件头：是不是 Ghost 镜像（完整校验交给 Ghost 自己）。

    纯文件读取，不 spawn 任何进程——计划期与界面探测都靠它，所以必须便宜。
    """
    target = Path(str(path)).expanduser()
    try:
        with target.open("rb") as handle:
            magic = handle.read(2)
        size = target.stat().st_size
    except OSError as exc:
        return {
            "path": str(target),
            "name": target.name,
            "ok": False,
            "exists": False,
            "reason": f"无法读取：{exc}",
        }
    if magic != GHO_MAGIC:
        return {
            "path": str(target),
            "name": target.name,
            "ok": False,
            "exists": True,
            "size_bytes": size,
            "reason": f"不是 Ghost 镜像（文件头 {magic.hex(' ') if magic else '空'}）",
        }
    return {
        "path": str(target),
        "name": target.name,
        "ok": True,
        "exists": True,
        "size_bytes": size,
        "block_index": DEFAULT_BLOCK_INDEX,
        "reason": "Ghost 分区块镜像",
    }


def parse_ghosterr(text: str, limit: int = 600) -> str:
    """``ghosterr.txt`` 动辄几十行，取末尾一段（Ghost 把致命错误写在最后）。"""
    body = (text or "").strip()
    return body[-limit:] if len(body) > limit else body


__all__ = [
    "DEFAULT_BLOCK_INDEX",
    "GHO_MAGIC",
    "GHO_SUFFIXES",
    "ghost_command",
    "parse_ghosterr",
    "sniff_gho",
]
