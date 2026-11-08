"""步骤与阶段契约：执行器、JobManager、前端共用同一份键。

**为什么单独一个模块**：以前 ``STEP_TITLES`` 住在 ``real_runner`` 里，
``jobs.py`` 反向 import 执行器才能算出"7 步进度"，步骤键成了跨层隐式协议。
现在这里是零依赖的纯数据：

- ``STEP_KEYS``：一次真实写盘的全部步骤（7 步）；
- ``STEP_GROUPS``：步骤分组——**分区组**（EFI / C / D / 分区验收）、
  **系统组**（镜像 / 引导并校验）与**收尾组**（附加文件拷贝）。
  写引导与校验引导合成同一个 ``boot`` 步骤，因为两者用的是同一组盘符、
  必须紧邻发生（见 :mod:`app.application.uefi`）；``copy`` 单独成组，
  因为它的失败语义不同（该盘流程没走完），不属于“写系统”。
- ``phases_payload()``：给 ``POST /api/prepare`` 的声明式编排，前端按它渲染，
  不再硬编码一份步骤表。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

# 分区组：``layout`` 是独立的一步——"分区验收（对照计划）"。
# 以前它被塞在 ``data`` 步骤的 details 里，进度条上看不出来，失败原因也藏在日志。
PARTITION_STEPS: tuple[str, ...] = ("efi", "windows", "data", "layout")
SYSTEM_STEPS: tuple[str, ...] = ("image", "boot")
# 收尾：把设置里的素材目录拷到第一数据分区根目录（设置留空 → 该步记 skipped）。
EXTRAS_STEPS: tuple[str, ...] = ("copy",)

STEP_GROUPS: dict[str, tuple[str, ...]] = {
    "partition": PARTITION_STEPS,
    "system": SYSTEM_STEPS,
    "extras": EXTRAS_STEPS,
}
GROUP_ORDER: tuple[str, ...] = ("partition", "system", "extras")
GROUP_TITLES: dict[str, str] = {
    "partition": "硬盘分区",
    "system": "写入系统",
    "extras": "收尾",
}
# 分组失败后不再执行后面的组——这是产品语义，不是实现细节。
# ``extras`` 是最后一组：保持 True 就得到“拷贝失败 = 整盘失败（流程没走完）”。
GROUP_STOP_ON_FAILURE: dict[str, bool] = {
    "partition": True,
    "system": True,
    "extras": True,
}

STEP_TITLES: dict[str, str] = {
    "efi": "创建 EFI 分区",
    "windows": "创建 Windows 分区",
    "data": "创建数据分区",
    "layout": "分区验收（对照计划）",
    "image": "应用 GHO 镜像（Ghost）",
    "boot": "写入 UEFI 引导并校验",
    "copy": "拷贝附加文件到数据分区",
}
STEP_KEYS: tuple[str, ...] = tuple(STEP_TITLES)
GROUP_OF: dict[str, str] = {
    step: group for group, steps in STEP_GROUPS.items() for step in steps
}


def phases_payload() -> list[dict[str, Any]]:
    """声明式编排：``POST /api/prepare`` 的 ``plan.phases``。

    前端按这份数据渲染分组进度；后端 job 进度也按同一份分组算百分比。
    """
    return [
        {
            "key": group,
            "title": GROUP_TITLES[group],
            "steps": list(STEP_GROUPS[group]),
            "stop_on_failure": GROUP_STOP_ON_FAILURE[group],
        }
        for group in GROUP_ORDER
    ]


def group_progress(
    completed: Sequence[str],
    current: str | None = None,
) -> dict[str, dict[str, Any]]:
    """按组算进度（纯函数）：给 ``job.progress.groups`` 用。

    ``completed`` 里的键可能包含未知的附加步骤（例如旧版本写入的
    历史事件），不属于任何组时忽略——进度分母只算真实阶段。
    """
    done = {str(key) for key in completed}
    groups: dict[str, dict[str, Any]] = {}
    for group in GROUP_ORDER:
        steps = STEP_GROUPS[group]
        finished = [step for step in steps if step in done]
        percent = round(len(finished) * 100 / len(steps)) if steps else 0
        groups[group] = {
            "title": GROUP_TITLES[group],
            "total": len(steps),
            "completed": finished,
            "current": current if current in steps else None,
            "percent": min(percent, 100),
            "done": len(finished) == len(steps),
        }
    return groups


def steps_after(group: str, key: str) -> tuple[str, ...]:
    """同组里排在 ``key`` 之后的步骤（失败时用来统一补 ``skipped``）。"""
    steps = STEP_GROUPS.get(group, ())
    if key not in steps:
        return steps
    return steps[steps.index(key) + 1 :]


def following_groups(group: str) -> tuple[str, ...]:
    """``group`` 之后的组（失败时整组跳过）。"""
    if group not in GROUP_ORDER:
        return ()
    return GROUP_ORDER[GROUP_ORDER.index(group) + 1 :]


__all__ = [
    "EXTRAS_STEPS",
    "GROUP_OF",
    "GROUP_ORDER",
    "GROUP_STOP_ON_FAILURE",
    "GROUP_TITLES",
    "PARTITION_STEPS",
    "STEP_GROUPS",
    "STEP_KEYS",
    "STEP_TITLES",
    "SYSTEM_STEPS",
    "following_groups",
    "group_progress",
    "phases_payload",
    "steps_after",
]
