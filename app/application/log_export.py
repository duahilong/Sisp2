"""日志导出：把「这一轮装机的结论 + 执行日志」打成一个 zip，方便发给客服排查。

## 为什么要做

现场的用户不会读 ``disk-runner-d2-20261109-171629-4456-693b62.log``：几百行
diskpart 命令、中文错误、多个文件。真正需要的信息只有五件事：

1. 哪几块盘（型号 + 盘号 + 容量）；
2. 每块盘走到哪一步、结果如何；
3. 失败的是哪一步、原文是什么；
4. 当时的诊断（分区间隔、其它盘在干什么、重试/续跑次数）；
5. 原始日志文件（真要深挖时）。

所以导出包的结构是：``summary.txt``（人读）+ ``summary.json``（机器读）+
``logs/*.log``（原始）+ ``ghosterr-*.txt``（Ghost 自带失败原因，有就带上）。
文件名全部 ASCII，内容 UTF-8，Windows 自带解压与记事本都能直接打开。

本模块只做「组装与写盘」，不弹对话框、不读设置；磁盘名统一走
:mod:`app.application.disk_label`，与界面/日志里的称呼一致。
"""

from __future__ import annotations

import json
import platform
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.application.disk_label import disk_label, model_of
from app.application.runner_utils import to_int, to_text

__all__ = [
    "ExportBundle",
    "ExportEntry",
    "build_bundle",
    "default_export_name",
]

#: zip 里最多带多少个日志文件（一轮多盘一般 2–8 个；上限只为防止误传目录）。
MAX_LOG_FILES = 40

_READ_ME = """\
这个压缩包是 Sisp2（硬盘工具箱）自动生成的排查包。

怎么用
  1. 先看 summary.txt：每块盘的型号、容量、每一步的结果、失败原因都在里面；
  2. 需要细节时再打开 logs/ 里对应那块盘的 .log（文件名里的 d2 表示磁盘 2）；
  3. 如果有 artifacts/ 目录，那是 Ghost 自己写的失败原因（GHOSTERR.TXT 之类）。

为什么日志里用「型号（磁盘 N）」而不是只写 disk1
  盘号会随插拔/换口变化，型号不会。两块同型号的盘靠括号里的盘号区分。

包里的文件
  summary.txt      人读的结论表（推荐先看这个）
  summary.json     同一份结论的机器可读版本（issue 里贴上来也行）
  logs/            原始执行日志（diskpart 命令、退出码、耗时、重试与续跑记录）
  artifacts/       Ghost 失败原因等附加文件（有才带）
  settings.json    当时的设置快照（不含密码类字段；只有本机路径）
"""


def default_export_name(now: datetime | None = None) -> str:
    """默认文件名：``Sisp2-日志-20261109-1720.zip``。"""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M")
    return f"Sisp2-日志-{stamp}.zip"


@dataclass
class ExportEntry:
    """一块盘（一个任务）在排查包里的那一行。"""

    job_id: str = ""
    disk_number: int = 0
    label: str = ""
    model: str = ""
    serial_number: str = ""
    size_display: str = ""
    status: str = ""
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    steps: list[dict[str, Any]] = field(default_factory=list)
    log_path: str = ""
    partition_plan: list[dict[str, Any]] = field(default_factory=list)

    def failed_steps(self) -> list[dict[str, Any]]:
        return [step for step in self.steps if str(step.get("status")) == "failed"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "disk_number": self.disk_number,
            "label": self.label,
            "model": self.model,
            "serial_number": self.serial_number,
            "size_display": self.size_display,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "steps": [dict(step) for step in self.steps],
            "log_path": self.log_path,
            "partition_plan": [dict(item) for item in self.partition_plan],
        }


@dataclass
class ExportBundle:
    """一次导出：结论条目 + 要打包的文件（日志 / 附加产物）。"""

    entries: list[ExportEntry] = field(default_factory=list)
    logs: list[tuple[str, Path]] = field(default_factory=list)
    artifacts: list[tuple[str, Path]] = field(default_factory=list)
    settings: Mapping[str, Any] | None = None
    version: str = ""
    log_root: str = ""
    generated_at: datetime = field(default_factory=datetime.now)

    # ------------------------------------------------------------------ 文本
    @property
    def machine(self) -> str:
        return f"{platform.platform()} / Python {platform.python_version()}"

    def default_name(self) -> str:
        return default_export_name(self.generated_at)

    def summary_text(self) -> str:
        """``summary.txt``：先结论、再逐盘原因，最后是读法说明。"""
        lines: list[str] = [
            "Sisp2 排查包",
            f"生成时间：{self.generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
            f"程序版本：{self.version or '(未知)'}",
            f"系统/环境：{self.machine}",
            f"日志目录：{self.log_root or '(未配置，日志可能只在内存里)'}",
            "",
        ]
        if not self.entries:
            lines.append("这一轮没有任务记录（可能还没开始执行）。")
        else:
            ok_count = sum(1 for entry in self.entries if entry.status == "ok")
            lines.append(f"一共 {len(self.entries)} 块盘：成功 {ok_count} 块，"
                         f"其它 {len(self.entries) - ok_count} 块")
            lines.append("")
            lines.extend(self._table())
            lines.append("")
            lines.extend(self._failures())
            lines.extend(self._partition_plans())
        lines.append("")
        lines.append(shared_hint())
        return "\n".join(lines)

    def _table(self) -> list[str]:
        header = ("硬盘", "盘号", "容量", "状态", "用时", "日志")
        rows: list[tuple[str, ...]] = [header]
        for entry in sorted(self.entries, key=lambda item: item.disk_number):
            rows.append(
                (
                    entry.model or "(未上报型号)",
                    str(entry.disk_number),
                    entry.size_display or "-",
                    entry.status or "-",
                    f"{entry.duration_seconds:.1f}s" if entry.duration_seconds else "-",
                    Path(entry.log_path).name if entry.log_path else "(无)",
                )
            )
        widths = [max(_width(row[i]) for row in rows) for i in range(len(header))]
        out: list[str] = []
        for index, row in enumerate(rows):
            cells = []
            for i, cell in enumerate(row):
                cells.append(cell + " " * max(0, widths[i] - _width(cell)))
            out.append("  ".join(cells).rstrip())
            if index == 0:
                out.append("  ".join("-" * width for width in widths))
        return out

    def _failures(self) -> list[str]:
        failing = [entry for entry in self.entries if entry.status != "ok"]
        if not failing:
            return ["没有失败的盘。"]
        out: list[str] = ["失败原因（按盘）："]
        for entry in sorted(failing, key=lambda item: item.disk_number):
            out.append(f"- {entry.label or disk_label(entry.disk_number, entry.model)}")
            if entry.error:
                out.append(f"    整单原因：{_one_line(entry.error)}")
            for step in entry.failed_steps():
                out.append(f"    失败步骤：{step.get('title') or step.get('key')}")
                out.append(f"      原文：{_one_line(step.get('message'))}")
                details = step.get("details") or {}
                for key, title in _DETAIL_TITLES:
                    value = details.get(key)
                    if value not in (None, "", [], {}):
                        out.append(f"      {title}：{value}")
        return out

    def _partition_plans(self) -> list[str]:
        out: list[str] = []
        for entry in sorted(self.entries, key=lambda item: item.disk_number):
            plan = entry.partition_plan
            if not plan:
                continue
            parts = []
            for item in plan:
                label = to_text(item.get("label")) or to_text(item.get("role")) or "?"
                # ``PlannedPartition.to_dict()`` 里大小的键叫 size_display
                # （``size_text`` 只是同一份数据的旧名字）。
                size = to_text(item.get("size_display")) or to_text(item.get("size_text")) or ""
                letter = to_text(item.get("letter")) or ""
                parts.append(" ".join(piece for piece in (label, size, f"({letter}:)" if letter else "") if piece))
            out.append(f"{entry.label or disk_label(entry.disk_number, entry.model)} 计划："
                       + " + ".join(parts))
        if out:
            out.insert(0, "分区计划：")
        return out

    def summary_json(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "generated_at": self.generated_at.isoformat(timespec="seconds"),
                "machine": self.machine,
                "log_root": self.log_root,
                "settings": dict(self.settings or {}),
                "entries": [entry.to_dict() for entry in self.entries],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )

    # ------------------------------------------------------------------ 写盘
    def write(self, path: str | Path) -> dict[str, Any]:
        """写到 ``path``；返回 ``{path, size, files}``（files 是包内文件名）。"""
        target = Path(path)
        if target.suffix.lower() != ".zip":
            target = target.with_suffix(".zip")
        target.parent.mkdir(parents=True, exist_ok=True)
        members: list[str] = []
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("README.txt", _READ_ME)
            members.append("README.txt")
            bundle.writestr("summary.txt", self.summary_text())
            members.append("summary.txt")
            bundle.writestr("summary.json", self.summary_json())
            members.append("summary.json")
            if self.settings is not None:
                bundle.writestr(
                    "settings.json",
                    json.dumps(dict(self.settings), ensure_ascii=False, indent=2, default=str),
                )
                members.append("settings.json")
            for arcname, source in self.logs:
                try:
                    bundle.write(source, arcname)
                except OSError:
                    continue
                members.append(arcname)
            for arcname, source in self.artifacts:
                try:
                    bundle.write(source, arcname)
                except OSError:
                    continue
                members.append(arcname)
        try:
            size = target.stat().st_size
        except OSError:
            size = 0
        return {"path": str(target), "size": size, "files": members}


def _one_line(value: Any, *, limit: int = 400) -> str:
    """把多行原文折成一行：summary 里断行会把缩进结构冲散。"""
    text = " ".join(str(value or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _width(text: str) -> int:
    """显示宽度：中日韩全角算 2（表格对齐用）。"""
    total = 0
    for char in str(text or ""):
        total += 2 if ord(char) > 0x2E80 else 1
    return total


#: 失败步骤详情里值得放进总结的键（其余留给原始日志）。
_DETAIL_TITLES: tuple[tuple[str, str], ...] = (
    ("diskpart_exit", "diskpart 退出码"),
    ("diskpart_attempts", "清盘尝试次数"),
    ("diskpart_repairs", "建分区续跑次数"),
    ("settle_ms", "diskpart 间隔（ms）"),
    ("parallel_activity", "当时其它盘在做"),
    ("guard_stage", "复核阶段"),
    ("guard_reasons", "复核不通过原因"),
)


def shared_hint() -> str:
    """给用户/客服的下一步建议（导出包与界面提示共用一句话）。"""
    return (
        "排查顺序：① 关掉该盘的资源管理器窗口与杀软实时扫描；② 换数据线/直连口；"
        "③ 看 SMART 的 C7（UltraDMA CRC）是否在涨——涨说明是线/口/桥接板，不是盘。"
        "把整个压缩包发给开发即可。"
    )


def build_bundle(
    jobs: Iterable[Any],
    *,
    job_ids: Sequence[str] | None = None,
    log_paths: Mapping[str, str] | None = None,
    log_root: str = "",
    version: str = "",
    settings: Mapping[str, Any] | None = None,
    artifacts: Sequence[tuple[str, Path]] = (),
    now: datetime | None = None,
    max_logs: int = MAX_LOG_FILES,
) -> ExportBundle:
    """把任务对象（或用例里传的 dict）转成导出包。

    ``jobs`` 里的元素既可以是 ``Job`` 对象，也可以是 ``job.to_dict()`` 的结果
    （测试与「任务已被清理」的场景都走后者）。``log_paths`` 用于运行中的任务
    ——它还没有 ``details.log_path``，由 API 层用 ``_latest_disk_log`` 兜底补上。
    """
    wanted = {str(item) for item in (job_ids or []) if str(item).strip()}
    mapping = dict(log_paths or {})
    entries: list[ExportEntry] = []
    logs: list[tuple[str, Path]] = []
    seen_names: set[str] = set()

    for job in jobs:
        data = job.to_dict() if hasattr(job, "to_dict") else dict(job or {})
        job_id = to_text(data.get("id")) or ""
        if wanted and job_id not in wanted:
            continue
        plan = data.get("plan") or {}
        spec = data.get("spec") or {}
        identity = spec.get("expected_identity") or {}
        result = data.get("result") or {}
        disk_number = to_int(data.get("disk_number")) or to_int(plan.get("disk_number")) or 0
        model = model_of(plan) or model_of(identity)
        label = (
            to_text(plan.get("label"))
            or to_text(identity.get("label"))
            or disk_label(disk_number, model, size_display=plan.get("size_display"))
        )
        log_path = to_text(mapping.get(job_id)) or _log_path_of(result) or ""
        entry = ExportEntry(
            job_id=job_id,
            disk_number=disk_number,
            label=label,
            model=model,
            serial_number=to_text(identity.get("serial_number")) or "",
            size_display=to_text(plan.get("size_display"))
            or to_text(identity.get("size_display"))
            or "",
            status=to_text(data.get("status")) or "",
            error=to_text(data.get("error")) or "",
            started_at=to_text(data.get("started_at")) or "",
            finished_at=to_text(data.get("finished_at")) or "",
            duration_seconds=_float(result.get("duration_seconds")),
            steps=[dict(step) for step in (result.get("steps") or []) if isinstance(step, Mapping)],
            log_path=log_path,
            partition_plan=[
                dict(item) for item in (plan.get("partition_plan") or []) if isinstance(item, Mapping)
            ],
        )
        entries.append(entry)

        if log_path and len(logs) < max(0, int(max_logs)):
            source = Path(log_path)
            name = f"logs/{source.name}"
            if source.is_file() and name not in seen_names:
                seen_names.add(name)
                logs.append((name, source))

    entries.sort(key=lambda item: (item.disk_number, item.job_id))
    return ExportBundle(
        entries=entries,
        logs=logs,
        artifacts=[(name, Path(path)) for name, path in artifacts if Path(path).is_file()][
            :MAX_LOG_FILES
        ],
        settings=settings,
        version=version,
        log_root=log_root,
        generated_at=now or datetime.now(),
    )


def _log_path_of(result: Mapping[str, Any]) -> str:
    """从 ``WorkerResult.to_dict()`` 的步骤详情里找日志路径（最后一步通常带）。"""
    for step in reversed(list(result.get("steps") or [])):
        if not isinstance(step, Mapping):
            continue
        details = step.get("details") or {}
        path = to_text(details.get("log_path"))
        if path:
            return path
    return ""


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
