"""分区级操作：新建 / 格式化 / 删除 / 分配盘符。

“装系统”是整盘重来（``clean`` + 三分区），本模块处理的是更细的场景：
某块数据盘上腾一个分区、格式化一个分区、删掉一个分区。

安全边界与整盘任务完全一致，而且更严：

1. 仍然先过 ``SispService.require_disk_writable``（dry-run / 白名单 /
   强身份 / 重新扫描后复核身份 / 保护规则），
2. **永远不碰** ESP、MSR、恢复分区和标了 ``IsSystem``/``IsBoot`` 的分区，
3. 分区号、大小、盘符全部做范围校验，用户输入不会直接拼进 diskpart 脚本，
4. ``dryRunByDefault`` 打开时只返回将要执行的脚本，不产生任何命令。

因此本模块只负责“把动作翻译成 diskpart 脚本 + 校验 + 执行 + 记日志”，
策略判断留给上层，方便用假 gate / 假 diskpart 做单元测试。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

BYTES_PER_MB = 1024 * 1024

DISKPART_TIMEOUT = 600.0

# 这些 GPT 类型不允许被本模块改动：动它们等于毁引导或毁恢复环境。
PROTECTED_GPT_TYPES: dict[str, str] = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "EFI 系统分区",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": "MS 保留分区",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "Windows 恢复分区",
}

# 文件系统：界面值 → diskpart 参数
FS_TOKENS: dict[str, str] = {
    "NTFS": "ntfs",
    "exFAT": "exfat",
    "FAT32": "fat32",
}

ACTION_KINDS = ("create", "format", "delete", "assign")

_ACTION_LABELS = {
    "create": "新建分区",
    "format": "格式化分区",
    "delete": "删除分区",
    "assign": "分配盘符",
}

_LABEL_RE = re.compile(r"[^0-9A-Za-z_\-\u4e00-\u9fff ]")


class PartitionError(ValueError):
    """动作不合法或执行失败时抛出的用户可读错误。"""


@dataclass(frozen=True)
class Action:
    """一条分区操作。字段按 kind 取用，其余留空。"""

    kind: str
    partition_number: int | None = None
    size_mb: int | None = None
    file_system: str = "NTFS"
    label: str = ""
    letter: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label_zh": _ACTION_LABELS.get(self.kind, self.kind),
            "partition_number": self.partition_number,
            "size_mb": self.size_mb,
            "file_system": self.file_system,
            "label": self.label,
            "letter": self.letter,
        }


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return number


def _clean_label(value: Any) -> str:
    text = _LABEL_RE.sub("", str(value or "")).strip()
    return text[:32]


def parse_actions(raw: Any) -> list[Action]:
    """把界面传来的动作列表收敛成 ``Action``；不合法直接抛错。"""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise PartitionError("actions 必须是数组")
    if not raw:
        raise PartitionError("没有任何分区操作")

    actions: list[Action] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise PartitionError(f"第 {index} 条操作格式不正确")
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in ACTION_KINDS:
            raise PartitionError(f"第 {index} 条操作类型不支持: {kind or '空'}")

        size_mb = _as_int(item.get("size_mb"))
        if size_mb is None:
            size_gb = item.get("size_gb")
            if size_gb not in (None, ""):
                try:
                    size_mb = int(round(float(size_gb) * 1024))
                except (TypeError, ValueError) as exc:
                    raise PartitionError(f"第 {index} 条操作的大小不是数字") from exc

        letter = str(item.get("letter") or "").strip().upper()[:1] or None
        if letter is not None and not letter.isalpha():
            raise PartitionError(f"第 {index} 条操作的盘符不合法: {letter}")

        raw_fs = str(item.get("file_system") or "NTFS").strip()
        fs = next((key for key in FS_TOKENS if key.lower() == raw_fs.lower()), "")
        if not fs:
            raise PartitionError(f"第 {index} 条操作的文件系统不支持: {raw_fs}")

        actions.append(
            Action(
                kind=kind,
                partition_number=_as_int(item.get("partition_number")),
                size_mb=size_mb,
                file_system=fs,
                label=_clean_label(item.get("label")),
                letter=letter,
            )
        )
    return actions


@dataclass
class DiskSnapshot:
    """校验需要的磁盘信息（来自一次实时扫描，不接受界面传入）。"""

    disk_number: int
    size_bytes: int
    partitions: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_summary(cls, summary: Mapping[str, Any]) -> DiskSnapshot:
        return cls(
            disk_number=_as_int(summary.get("disk_number")) or 0,
            size_bytes=_as_int(summary.get("size_bytes")) or 0,
            partitions=[
                dict(part)
                for part in (summary.get("partitions") or [])
                if isinstance(part, Mapping)
            ],
        )

    @property
    def free_bytes(self) -> int:
        used = sum(_as_int(part.get("size_bytes")) or 0 for part in self.partitions)
        # GPT 本身占 1 MB 左右，留 16 MB 余量，避免算到最后一兆字节。
        return max(self.size_bytes - used - 16 * BYTES_PER_MB, 0)

    def partition(self, number: int | None) -> dict[str, Any] | None:
        for part in self.partitions:
            if _as_int(part.get("partition_number")) == number:
                return part
        return None

    @property
    def used_letters(self) -> set[str]:
        letters = {
            str(part.get("drive_letter") or "").strip().upper()
            for part in self.partitions
            if part.get("drive_letter")
        }
        letters.discard("")
        return letters


def protected_reason(partition: Mapping[str, Any]) -> str | None:
    """ESP / MSR / 恢复分区 / 系统分区 / 引导分区 → 返回原因。"""
    gpt_type = str(partition.get("gpt_type") or "").strip("{}").lower()
    for guid, name in PROTECTED_GPT_TYPES.items():
        if gpt_type.startswith(guid):
            return name
    if partition.get("is_system"):
        return "系统分区"
    if partition.get("is_boot") and not partition.get("drive_letter"):
        return "引导分区"
    return None


def validate(
    actions: Sequence[Action],
    disk: DiskSnapshot,
    *,
    forbidden_letters: Iterable[str] = (),
) -> list[str]:
    """返回阻断项；空列表表示可以执行。纯函数，便于单元测试。"""
    blockers: list[str] = []
    if not actions:
        return ["没有任何分区操作"]

    occupied = disk.used_letters | {str(letter).upper() for letter in forbidden_letters}
    pending_letters: set[str] = set()
    free_bytes = disk.free_bytes

    for action in actions:
        if action.kind == "create":
            if action.size_mb is None or action.size_mb <= 0:
                blockers.append("新建分区必须给出大小（MB）")
                continue
            if action.size_mb * BYTES_PER_MB > free_bytes:
                blockers.append(
                    f"新建分区需要 {action.size_mb} MB，磁盘只剩 {free_bytes // BYTES_PER_MB} MB 未分配"
                )
            free_bytes -= action.size_mb * BYTES_PER_MB
            if action.letter:
                if action.letter in occupied or action.letter in pending_letters:
                    blockers.append(f"盘符 {action.letter} 已被占用")
                pending_letters.add(action.letter)
            continue

        if action.partition_number is None:
            blockers.append(f"{_ACTION_LABELS[action.kind]}必须指定分区号")
            continue
        part = disk.partition(action.partition_number)
        if part is None:
            blockers.append(f"分区 {action.partition_number} 不存在")
            continue
        reason = protected_reason(part)
        if reason is not None:
            blockers.append(f"分区 {action.partition_number} 是 {reason}，拒绝操作")
        if action.kind == "assign":
            if not action.letter:
                blockers.append("分配盘符必须给出一个盘符")
            elif action.letter in occupied or action.letter in pending_letters:
                blockers.append(f"盘符 {action.letter} 已被占用")
            else:
                pending_letters.add(action.letter)

    return list(dict.fromkeys(blockers))


def build_script(disk_number: int, actions: Sequence[Action]) -> list[str]:
    """生成 diskpart 脚本行；所有变量都来自已校验的 ``Action``。"""
    lines = [f"REM === 分区操作：硬盘 {disk_number} ===", f"select disk {int(disk_number)}"]
    for action in actions:
        if action.kind == "create":
            lines.append(f"create partition primary size={int(action.size_mb or 0)}")
            lines.append(f"format quick fs={FS_TOKENS[action.file_system]} label={_format_label(action)}")
            if action.letter:
                lines.append(f"assign letter={action.letter}")
        elif action.kind == "format":
            lines.append(f"select partition {int(action.partition_number or 0)}")
            lines.append(f"format quick fs={FS_TOKENS[action.file_system]} label={_format_label(action)}")
        elif action.kind == "delete":
            lines.append(f"select partition {int(action.partition_number or 0)}")
            lines.append("delete partition override")
        elif action.kind == "assign":
            lines.append(f"select partition {int(action.partition_number or 0)}")
            lines.append(f"assign letter={action.letter}")
    lines.append("exit")
    return lines


def _format_label(action: Action) -> str:
    # diskpart 的 label 不接受引号，也不接受空格开头的值；空值用占位符。
    label = action.label.replace(" ", "_")
    return f'"{label}"' if label else '""'


def _step_titles(actions: Sequence[Action]) -> list[str]:
    return [f"{_ACTION_LABELS[action.kind]}" for action in actions]


def _tail(text: str, limit: int = 2000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _mask(path: Path | None) -> str:
    return str(path) if path else ""


class PartitionRunner:
    """执行分区操作；``gate`` 是 ``SispService.require_disk_writable``。"""

    def __init__(
        self,
        gate: Callable[..., Mapping[str, Any]] | None = None,
        *,
        diskpart: str | None = None,
        temp_dir: str | None = None,
        log_dir: str | None = None,
        dry_run: bool = False,
        which: Callable[[str], str | None] | None = None,
        used_letters: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self._gate = gate
        self._which = which or shutil.which
        self.diskpart = diskpart or self._which("diskpart.exe")
        self.temp_dir = temp_dir
        self.log_dir = log_dir
        self.dry_run = bool(dry_run)
        self._used_letters = used_letters

    # ------------------------------------------------------------------ 入口
    def run(
        self,
        disk_number: int,
        actions: Any,
        *,
        identity: Mapping[str, Any] | None = None,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """校验并执行；返回脚本、退出码、日志位置，界面据此渲染结果。"""
        parsed = parse_actions(actions)
        effective_dry_run = self.dry_run if dry_run is None else bool(dry_run)

        summary = self._resolve_disk(disk_number, identity, effective_dry_run)
        disk = DiskSnapshot.from_summary(summary)
        blockers = validate(parsed, disk, forbidden_letters=self._forbidden_letters())
        if blockers:
            raise PartitionError("；".join(blockers))

        script = build_script(disk.disk_number or int(disk_number), parsed)
        result: dict[str, Any] = {
            "disk_number": disk.disk_number or int(disk_number),
            "dry_run": effective_dry_run,
            "script": list(script),
            "actions": [action.to_dict() for action in parsed],
            "steps": _step_titles(parsed),
            "exit": 0,
            "output": "",
            "log_path": "",
        }
        if effective_dry_run:
            result["message"] = "演练模式：只生成 diskpart 脚本，未执行任何命令"
            return result

        if not self.diskpart:
            raise PartitionError("找不到 diskpart.exe，无法执行分区操作")

        log = self._open_log(disk.disk_number or int(disk_number))
        log.write("diskpart 脚本：\n" + "\n".join(script))
        try:
            code, output = self._run_diskpart(script)
        finally:
            log.flush()

        result["exit"] = code
        result["output"] = _tail(output)
        result["log_path"] = _mask(log.path)
        if code != 0:
            log.write(f"diskpart 失败（退出码 {code}）：{_tail(output)}")
            raise PartitionError(f"diskpart 失败（退出码 {code}）：{_tail(output, 800)}")
        log.write("diskpart 完成")
        result["message"] = "分区操作已完成"
        return result

    # ------------------------------------------------------------------ 内部
    def _resolve_disk(
        self,
        disk_number: int,
        identity: Mapping[str, Any] | None,
        dry_run: bool,
    ) -> Mapping[str, Any]:
        """真实执行必须过闸门；演练模式只读地拿一份当前快照。"""
        if dry_run:
            summary = self._scan(disk_number)
            if summary is None:
                raise PartitionError(f"硬盘 {disk_number} 当前不存在")
            return summary
        if self._gate is None:
            raise PartitionError("缺少写盘闸门：分区操作必须经过 SispService 的白名单校验")
        return self._gate(int(disk_number), dict(identity or {}))

    def _scan(self, disk_number: int) -> Mapping[str, Any] | None:
        from app.modules.disk_info.service import scan_disk_summaries

        for summary in scan_disk_summaries():
            if _as_int(summary.get("disk_number")) == int(disk_number):
                return summary
        return None

    def _forbidden_letters(self) -> set[str]:
        if self._used_letters is None:
            return set()
        try:
            return {str(letter).upper() for letter in self._used_letters()}
        except Exception:  # noqa: BLE001 - 盘符探测失败不能挡住校验
            return set()

    def _run_diskpart(self, lines: Sequence[str]) -> tuple[int, str]:
        assert self.diskpart
        handle, script_path = tempfile.mkstemp(
            prefix="sisp-partition-", suffix=".txt", dir=self.temp_dir
        )
        Path(script_path).write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        try:
            completed = subprocess.run(  # noqa: S603 - 固定命令 + 已校验脚本
                [self.diskpart, "/s", script_path],
                capture_output=True,
                timeout=DISKPART_TIMEOUT,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
            )
            output = (completed.stdout or b"").decode("mbcs", errors="replace") + (
                completed.stderr or b""
            ).decode("mbcs", errors="replace")
            return completed.returncode, output
        except subprocess.TimeoutExpired as exc:
            raise PartitionError(f"diskpart 超时（{DISKPART_TIMEOUT:.0f} 秒），已终止") from exc
        except OSError as exc:
            raise PartitionError(f"无法启动 diskpart：{exc}") from exc
        finally:
            try:
                Path(script_path).unlink()
            except OSError:
                pass

    def _open_log(self, disk_number: int) -> _RunLog:
        return _RunLog(self.log_dir, f"partition-disk{disk_number}")


class _RunLog:
    """极简运行日志：每次操作一个文件，内容可复制给运维。"""

    def __init__(self, log_dir: str | None, prefix: str) -> None:
        self.lines: list[str] = []
        self.path: Path | None = None
        if not log_dir:
            return
        try:
            base = Path(log_dir)
            base.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.path = base / f"{prefix}-{stamp}.log"
            self._handle = self.path.open("w", encoding="utf-8")
        except OSError:
            self.path = None
            self._handle = None  # type: ignore[assignment]

    def write(self, text: str) -> None:
        self.lines.append(text)
        handle = getattr(self, "_handle", None)
        if handle is not None:
            handle.write(text + "\n")
            handle.flush()

    def flush(self) -> None:
        handle = getattr(self, "_handle", None)
        if handle is not None:
            handle.close()
            self._handle = None
