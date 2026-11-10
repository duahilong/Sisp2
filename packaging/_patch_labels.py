"""一次性补丁：把「硬盘 N」换成「型号（磁盘 N）」，并给执行计划加 label。"""

from __future__ import annotations

from pathlib import Path

WF = Path("app/application/workflow.py")
SVC = Path("app/application/service.py")
JOBS = Path("app/application/jobs.py")


def replace_once(text: str, old: str, new: str, *, where: str = "") -> str:
    if old not in text:
        raise SystemExit(f"[{where}] 未找到待替换片段：\n{old[:200]}")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------- workflow.py
wf = WF.read_text(encoding="utf-8")

wf = replace_once(
    wf,
    "from app.application.contracts import DiskInfo",
    "from app.application.contracts import DiskInfo\nfrom app.application.disk_label import disk_label",
    where="workflow-import",
)

wf = replace_once(
    wf,
    '''    try:
        parts = plan_partitions(payload)
    except PartitionLayoutError as exc:
        return [f"硬盘 {disk.disk_number} 分区配置无效：{exc}"]
    allow_small = allow_small_partitions(payload)
    problems = [
        f"硬盘 {disk.disk_number} 容量不足：{problem}"
        for problem in check_layout(parts, disk.size_bytes)
    ]
    problems += [
        f"硬盘 {disk.disk_number} 分区过小：{problem}"
        for problem in check_minimums(parts, disk.size_bytes, allow_small=allow_small)
    ]
    return problems''',
    '''    title = disk_label(disk.disk_number, disk.model, size_display=disk.size_display)
    try:
        parts = plan_partitions(payload)
    except PartitionLayoutError as exc:
        return [f"{title} 分区配置无效：{exc}"]
    allow_small = allow_small_partitions(payload)
    problems = [
        f"{title} 容量不足：{problem}"
        for problem in check_layout(parts, disk.size_bytes)
    ]
    problems += [
        f"{title} 分区过小：{problem}"
        for problem in check_minimums(parts, disk.size_bytes, allow_small=allow_small)
    ]
    return problems''',
    where="layout-problems",
)

# prepare() 里统一先算出 title，再替换后面的 6 处 blocker 文案。
wf = replace_once(
    wf,
    '''        # 分区布局：界面第 4 步直接渲染这份计划（真实大小 + 盘符 + 文件系统）。''',
    '''        title = disk_label(disk.disk_number, disk.model, size_display=disk.size_display)

        # 分区布局：界面第 4 步直接渲染这份计划（真实大小 + 盘符 + 文件系统）。''',
    where="prepare-title",
)

for old, new in (
    ('blockers.append(f"硬盘 {disk.disk_number} 不可操作：{reasons}")',
     'blockers.append(f"{title} 不可操作：{reasons}")'),
    ('blockers.append(f"硬盘 {disk.disk_number} 不在写盘白名单内")',
     'blockers.append(f"{title} 不在写盘白名单内")'),
    ('blockers.append(f"硬盘 {disk.disk_number} 盘符分配不完整")',
     'blockers.append(f"{title} 盘符分配不完整")'),
    ('blockers.append(f"硬盘 {disk.disk_number} 分区配置无效：{exc}")',
     'blockers.append(f"{title} 分区配置无效：{exc}")'),
):
    wf = replace_once(wf, old, new, where="blockers")

wf = replace_once(
    wf,
    '''        plan = ExecutionPlan(
            disk_number=disk.disk_number,
            model=disk.model or "",''',
    '''        plan = ExecutionPlan(
            disk_number=disk.disk_number,
            label=title,
            model=disk.model or "",''',
    where="plan-label",
)
WF.write_text(wf, encoding="utf-8")

# ---------------------------------------------------------------- jobs.py
jobs = JOBS.read_text(encoding="utf-8")
jobs = replace_once(
    jobs,
    '''    disk_number: int
    model: str = ""
    size_display: str = ""''',
    '''    disk_number: int
    #: 界面/导出统一用的显示名（``Msft Virtual Disk（磁盘 2）``）；老调用方不传
    #: 时由界面自己按 ``磁盘 N`` 兜底。
    label: str = ""
    model: str = ""
    size_display: str = ""''',
    where="plan-dataclass",
)
jobs = replace_once(
    jobs,
    '''        return {
            "disk_number": self.disk_number,
            "model": self.model,''',
    '''        return {
            "disk_number": self.disk_number,
            "label": self.label,
            "model": self.model,''',
    where="plan-to-dict",
)
JOBS.write_text(jobs, encoding="utf-8")

# ---------------------------------------------------------------- service.py
svc = SVC.read_text(encoding="utf-8")
svc = replace_once(
    svc,
    "from app.application.contracts import DiskInfo, SelectionPlan",
    "from app.application.contracts import DiskInfo, SelectionPlan\n"
    "from app.application.disk_label import disk_label",
    where="service-import",
)
svc = replace_once(
    svc,
    '''        summary = self._find_disk_summary(disk_number)
        if summary is None:
            raise PermissionError(f"硬盘 {disk_number} 当前不存在，拒绝写盘")
        actual = build_worker_disk_identity(summary)''',
    '''        summary = self._find_disk_summary(disk_number)
        if summary is None:
            raise PermissionError(f"硬盘 {disk_number} 当前不存在，拒绝写盘")
        title = disk_label(disk_number, summary.get("model"), size_display=summary.get("size_display"))
        actual = build_worker_disk_identity(summary)''',
    where="service-title",
)
svc = replace_once(
    svc,
    '''        if not identity_matches(dict(expected), actual, require_strong=False):
            raise PermissionError(
                f"硬盘 {disk_number} 身份与确认时不一致，拒绝写盘"
            )''',
    '''        if not identity_matches(dict(expected), actual, require_strong=False):
            raise PermissionError(
                f"{title} 身份与确认时不一致（型号/序列号/容量对不上），拒绝写盘"
            )''',
    where="service-identity",
)
svc = replace_once(
    svc,
    '''        if reasons:
            raise PermissionError(
                f"硬盘 {disk_number} 受保护，拒绝写盘：{'、'.join(reasons)}"
            )''',
    '''        if reasons:
            raise PermissionError(f"{title} 受保护，拒绝写盘：{'、'.join(reasons)}")''',
    where="service-protection",
)
SVC.write_text(svc, encoding="utf-8")
print("workflow/jobs/service patched")
